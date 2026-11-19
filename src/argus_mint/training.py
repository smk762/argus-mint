"""Training an embodied identity's LoRA: candidates, curator, lens, forge and proof, one step at a time.

Each identity version that carries a reviewed ``training`` block can be trained once. The
moderator advances it step by step (no step starts another), and every step is a job whose
record is kept, inputs, the sibling's run or scan id, outputs and error included:

1. ``candidates``: ``training.candidates`` renders of the identity's approved prompt through
   darkroom, on one checkpoint, no LoRA, at ``batch`` priority, with seeds derived from the
   project, identity, version and number (hard rule 7). A safety rejection is retried with the
   next derived seed and every attempt is recorded (hard rule 3).
2. ``select`` (argus-curator): the candidates are uploaded and scanned with faces on; curator's
   ``face_source`` filter keeps the images of one identity (anchored on the identity's own
   references, when it lists any), and mint takes the closest ``train`` + ``held_out`` of them
   and splits them: ``held_out`` evenly through the ranking go to the references, the rest to
   training. Both are exported by curator (``selection``), the references into
   ``<proof export>/references``. Curator has no consistency measure for a subject without a
   face, so a ``likeness`` identity stops here (recorded, not worked around).
3. ``caption`` (argus-lens): lens captions the training images (uploads; its ``training``
   variant, not the ``identity`` one its sidecars default to), and mint writes each caption as
   ``<stem>.txt`` with the trigger word first.
4. ``train`` (argus-forge): ``POST /config`` then ``POST /run``, polled to the end. Only a forge
   with training enabled (the trusted GPU host) takes it. The LoRA is hashed and copied into
   the LoRA directory darkroom and proof share.
5. ``evaluate`` (argus-proof): ``POST /run/stream`` with the LoRA, against the export's held-out
   references, then ``GET /report/{run}``: passing means proof's verdict passed (not pending a
   person's ratings) and the identity's ``check_metric`` was scored, at least ``min_score``
   when one is set.
6. ``pin``: the report is read again (ratings made in proof since count), the LoRA's sha256 is
   checked against proof's ``GET /report/{run}/manifest``, and a new **draft** version of the
   identity is saved with ``lora``, ``proof_run`` and ``proof_export`` pinned. The moderator
   reviews it like any project file; the approved file it came from never changes.

A step starts only when the step before it is ``done``, never while it (or any step of the
same training) is queued or running, and never again once it is ``done``; a failed step can be
started again and resumes what it can (rendered candidates stay; a forge or proof run already
started is looked up, not started twice). A job a restart cut off reads as failed.

Stored outside the repository::

    <ARGUS_MINT_TRAINING_DIR>/<project>/<identity>-v<version>/
      record.json                       # TrainingRecord
      candidates/<identity>-v<version>-01.png

The exports curator writes, forge trains from and proof reads live on a volume all four share
(``ARGUS_MINT_TRAINING_EXPORTS_DIR`` here): ``mint-<project>-<identity>-v<version>`` (training
images, their captions, curator's manifest, forge's output) and ``…-proof`` (``references/``
plus a copy of the training manifest, so proof's held-out check sees the training set).

The clients are thin and injected (:class:`Curator`, :class:`Lens`, :class:`Forge`,
:class:`Proof`; the HTTP ones are in ``training_clients``), so this module imports no network
client.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from argus_mint._codec import hex0x
from argus_mint.cards import write_atomic
from argus_mint.projects import Identity, IdentityTraining, Project
from argus_mint.render import Darklight, Rendered, RenderError, RenderFailed, render_requests
from argus_mint.templates import SEED_MAX, RenderRequest

__all__ = [
    "STEPS",
    "Candidate",
    "Caption",
    "Curator",
    "Evaluation",
    "FailedCandidate",
    "Forge",
    "Lens",
    "Pinned",
    "Proof",
    "Selection",
    "StepRun",
    "TrainedLora",
    "TrainingContext",
    "TrainingError",
    "TrainingRecord",
    "TrainingStore",
    "candidate_request",
    "candidate_seed",
    "check_held_out",
    "export_names",
    "judge",
    "rank_kept",
    "run_step",
    "split_held_out",
]

STEPS: tuple[str, ...] = ("candidates", "select", "caption", "train", "evaluate", "pin")
Step = Literal["candidates", "select", "caption", "train", "evaluate", "pin"]
StepStatus = Literal["queued", "running", "done", "failed"]
RECORD = "record.json"
CANDIDATES = "candidates"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class TrainingError(ValueError):
    """A training step can't start, or stopped."""


# -- the sibling clients ----------------------------------------------------------------------------


class Curator(Protocol):
    """argus-curator (docs/http-api.md): ``POST /upload``, ``POST /scan/folder``, ``POST /scan/{id}/select``,
    ``POST /export``. Each returns curator's JSON as is."""

    def upload(self, folder: str, files: list[tuple[str, bytes]]) -> dict[str, Any]: ...
    def scan(self, body: dict[str, Any]) -> dict[str, Any]: ...
    def select(self, scan_id: str, body: dict[str, Any]) -> dict[str, Any]: ...
    def export(self, body: dict[str, Any]) -> dict[str, Any]: ...


class Lens(Protocol):
    """argus-lens: ``POST /caption/batch`` (multipart uploads) -> ``{results: {name: CaptionResult}, errors}``."""

    def caption_batch(self, files: list[tuple[str, bytes]], form: dict[str, str]) -> dict[str, Any]: ...


class Forge(Protocol):
    """argus-forge: ``GET /health``, ``POST /config``, ``POST /run``, ``GET /run/{id}`` (None: no such run)."""

    def health(self) -> dict[str, Any]: ...
    def config(self, body: dict[str, Any]) -> dict[str, Any]: ...
    def run(self, body: dict[str, Any]) -> dict[str, Any]: ...
    def status(self, run_id: str) -> dict[str, Any] | None: ...


class Proof(Protocol):
    """argus-proof: ``POST /run/stream`` (returns its last frame, ``complete`` or ``error``),
    ``GET /report/{run}`` (None: no report yet) and ``GET /report/{run}/manifest``."""

    def run(self, body: dict[str, Any]) -> dict[str, Any]: ...
    def report(self, run_id: str) -> dict[str, Any] | None: ...
    def manifest(self, run_id: str) -> dict[str, Any]: ...


# -- the record --------------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StepRun(_Model):
    """One run of one step: what it was given, which sibling run or scan it made, what came of it."""

    step: Step
    job: str = Field(pattern=r"^[0-9a-f]{16}$")
    status: StepStatus
    started_at: datetime
    finished_at: datetime | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    sibling: dict[str, str] = Field(default_factory=dict)  # e.g. {"curator_scan": …}, {"forge_run": …}
    outputs: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class Candidate(_Model):
    n: int = Field(ge=1)
    seed: int  # the seed asked for; the request's is the final one
    file: str  # relative to the record's directory
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: RenderRequest
    attempts: tuple[dict[str, Any], ...]
    made_with: dict[str, str] = Field(default_factory=dict)


class FailedCandidate(_Model):
    """A candidate that made no image. ``final``: the safety check refused every attempt."""

    n: int = Field(ge=1)
    seed: int
    error: str
    final: bool
    attempts: tuple[dict[str, Any], ...]


class Selection(_Model):
    """Curator's pick, split by mint. Files are candidate files; the two sets never share one."""

    scan_id: str
    kept: int  # how many curator's filter kept
    train: tuple[str, ...]
    held_out: tuple[str, ...]
    anchors: tuple[str, ...] = ()  # the identity's references curator measured distance to
    train_export: str  # under the shared exports directory (forge's export_dir)
    proof_export: str  # the directory name proof knows the references by
    train_sha256: tuple[str, ...]
    held_out_sha256: tuple[str, ...]


class Caption(_Model):
    file: str  # the training image, relative to the training export
    text: str


class TrainedLora(_Model):
    name: str  # relative to the shared LoRA directory: what darkroom and proof load
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    forge_run: str
    output_name: str
    trigger: str


class Evaluation(_Model):
    proof_run: str
    metric: Literal["identity", "likeness"]
    score: float | None  # the metric's mean over proof's images; None: it wasn't scored
    min_score: float | None
    proof_passed: bool  # proof's own verdict
    pending: bool  # proof is waiting on a person's ratings
    passed: bool  # what mint pins on: see judge()
    reasons: tuple[str, ...] = ()


class Pinned(_Model):
    identity_version: int  # the draft version saved for review
    lora: str
    sha256: str
    proof_run: str
    proof_export: str


class TrainingRecord(_Model):
    """``record.json``: one identity version's training, every step run kept."""

    format: Literal[1] = 1
    project: str
    identity: str
    identity_version: int = Field(ge=1)
    identity_hash: str = Field(pattern=r"^0x[0-9a-f]{64}$")
    metric: Literal["identity", "likeness"]
    runs: tuple[StepRun, ...] = ()  # oldest first
    candidates: tuple[Candidate, ...] = ()
    failed: tuple[FailedCandidate, ...] = ()
    selection: Selection | None = None
    captions: tuple[Caption, ...] = ()
    lora: TrainedLora | None = None
    evaluation: Evaluation | None = None
    pinned: Pinned | None = None

    def last(self, step: str) -> StepRun | None:
        return next((r for r in reversed(self.runs) if r.step == step), None)


def export_names(project: str, identity: str, version: int) -> tuple[str, str]:
    """(training export, proof export): directory names under the shared exports volume."""
    base = f"mint-{project}-{identity}-v{version}"
    return base, f"{base}-proof"


def candidate_file(identity: Identity, n: int) -> str:
    return f"{CANDIDATES}/{identity.id}-v{identity.version}-{n:02d}.png"


def candidate_seed(project: str, identity: Identity, n: int) -> int:
    """The seed asked for candidate ``n``: the same every time (hard rule 7)."""
    digest = hashlib.sha256(
        f"argus-mint/identity-candidate/{project}/{identity.id}/{identity.version}/{n}".encode()
    ).digest()
    return int.from_bytes(digest[:4], "big") & SEED_MAX


def candidate_request(identity: Identity, seed: int) -> RenderRequest:
    """One candidate: the identity's fixed prompt on its training checkpoint, no LoRA (hard rule 1)."""
    t = _training(identity)
    return RenderRequest(
        template_id=identity.id,
        template_version=identity.version,
        template_hash=identity.hash(),
        model=t.model,
        loras=(),
        sampling=t.sampling,
        width=t.width,
        height=t.height,
        prompt=identity.prompt,
        negative=identity.negative,
        variables={},
        seed=seed,
    )


def _training(identity: Identity) -> IdentityTraining:
    if identity.training is None:
        raise TrainingError(f"identity {identity.id} v{identity.version} has no `training` block")
    return identity.training


# -- the store ---------------------------------------------------------------------------------------


class TrainingStore:
    """Training records under ``root``, one directory per identity version. ``lock`` serialises writes."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.lock = threading.RLock()

    def dir(self, project: str, identity: str, version: int) -> Path:
        if not _ID.fullmatch(project) or not _ID.fullmatch(identity) or version < 1:
            raise TrainingError(f"not an identity: {project}/{identity} v{version}")
        return self.root / project / f"{identity}-v{version}"

    def load(self, project: str, identity: str, version: int) -> TrainingRecord | None:
        f = self.dir(project, identity, version) / RECORD
        if not f.is_file():
            return None
        try:
            return TrainingRecord.model_validate(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError) as e:
            raise TrainingError(f"{f}: {e}") from e

    def save(self, r: TrainingRecord) -> None:
        d = self.dir(r.project, r.identity, r.identity_version)
        d.mkdir(parents=True, exist_ok=True)
        write_atomic(d / RECORD, r.model_dump_json(indent=2) + "\n")

    def start(self, project: str, identity: Identity) -> TrainingRecord:
        """The record for ``identity``, created when it has none. Refuses one made from other file text."""
        r = self.load(project, identity.id, identity.version)
        want = hex0x(identity.hash())
        if r is None:
            if identity.check_metric is None:
                raise TrainingError(f"identity {identity.id} v{identity.version} has no `check_metric`")
            r = TrainingRecord(
                project=project, identity=identity.id, identity_version=identity.version,
                identity_hash=want, metric=identity.check_metric,
            )  # fmt: skip
        elif r.identity_hash != want:
            raise TrainingError(f"the training of {identity.id} v{identity.version} was started from another file")
        return r

    def update(self, project: str, identity: str, version: int, **fields: Any) -> TrainingRecord:
        with self.lock:
            r = self.load(project, identity, version)
            if r is None:
                raise TrainingError(f"no training record for {identity} v{version}")
            r = r.model_copy(update=fields)
            TrainingRecord.model_validate(r.model_dump())
            self.save(r)
            return r

    def set_run(self, project: str, identity: str, version: int, run: StepRun) -> TrainingRecord:
        """Add ``run``, or replace the run with its job id."""
        with self.lock:
            r = self.load(project, identity, version)
            if r is None:
                raise TrainingError(f"no training record for {identity} v{version}")
            runs = [x for x in r.runs if x.job != run.job]
            idx = next((i for i, x in enumerate(r.runs) if x.job == run.job), len(runs))
            runs.insert(idx, run)
            r = r.model_copy(update={"runs": tuple(runs)})
            self.save(r)
            return r

    def all(self) -> list[TrainingRecord]:
        out = []
        if not self.root.is_dir():
            return out
        for f in sorted(self.root.glob(f"*/*/{RECORD}")):
            try:
                out.append(TrainingRecord.model_validate(json.loads(f.read_text(encoding="utf-8"))))
            except (OSError, ValueError, ValidationError):
                continue
        return out

    def candidate_image(self, r: TrainingRecord, n: int) -> bytes:
        c = next((c for c in r.candidates if c.n == n), None)
        if c is None:
            raise TrainingError(f"{r.identity} v{r.identity_version} has no candidate {n}")
        data = (self.dir(r.project, r.identity, r.identity_version) / c.file).read_bytes()
        if hashlib.sha256(data).hexdigest() != c.sha256:
            raise TrainingError(f"candidate {n} on disk differs from the one rendered")
        return data


def step_state(r: TrainingRecord | None, live: set[str]) -> dict[str, str]:
    """Each step's status: ``not_started``, or its last run's (a queued or running run this process
    doesn't own was cut off by a restart, and reads as ``failed``)."""
    out: dict[str, str] = {}
    for step in STEPS:
        last = r.last(step) if r is not None else None
        if last is None:
            out[step] = "not_started"
        elif last.status in ("queued", "running") and last.job not in live:
            out[step] = "failed"
        else:
            out[step] = last.status
    return out


def refusal(r: TrainingRecord | None, step: str, live: set[str]) -> str | None:
    """Why ``step`` can't start now, or None: the step before it must be done, nothing of this
    training may be queued or running, and a done step never runs again."""
    if step not in STEPS:
        return f"no training step {step!r} (steps: {', '.join(STEPS)})"
    state = step_state(r, live)
    busy = [s for s, v in state.items() if v in ("queued", "running")]
    if busy:
        return f"step {busy[0]} is {state[busy[0]]}; one step at a time"
    if state[step] == "done":
        return f"step {step} is done; a finished step isn't run again"
    i = STEPS.index(step)
    if i > 0 and state[STEPS[i - 1]] != "done":
        return f"step {step} waits for {STEPS[i - 1]} ({state[STEPS[i - 1]].replace('_', ' ')})"
    return None


# -- pure helpers --------------------------------------------------------------------------------------


def rank_kept(scan: dict[str, Any], kept: list[str]) -> list[str]:
    """Curator's kept images, closest to the identity first: the primary face's reference distance
    (or its cluster distance without references), then curator's score, then the path."""
    by_path = {r.get("rel_path"): r for r in scan.get("results") or [] if isinstance(r, dict)}

    def key(rel: str) -> tuple[float, float, str]:
        r = by_path.get(rel) or {}
        face = next((f for f in r.get("faces") or [] if f.get("primary")), None) or {}
        d = face.get("reference_distance")
        if d is None:
            d = face.get("cluster_distance")
        return (float(d) if d is not None else 9.0, -float(r.get("score") or 0.0), rel)

    return sorted(kept, key=key)


def split_held_out(ranked: list[str], train: int, held_out: int) -> tuple[list[str], list[str]]:
    """The closest ``train + held_out`` images, split: ``held_out`` spread evenly through the ranking
    (so the references are as close to the identity as the training set is), the rest to train."""
    top = ranked[: train + held_out]
    if len(top) < train + held_out:
        raise TrainingError(f"curator kept {len(ranked)} images; {train} to train and {held_out} held out need more")
    m = len(top)
    picks = {int((j + 0.5) * m / held_out) for j in range(held_out)}
    held = [x for i, x in enumerate(top) if i in picks]
    return [x for x in top if x not in held], held


def _images(d: Path) -> list[Path]:
    """Images directly in ``d`` (not forge's output, not references)."""
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES) if d.is_dir() else []


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def check_held_out(train_dir: Path, references_dir: Path) -> None:
    """Refuse when a held-out reference is a training image (by sha256), or either set is empty."""
    train = {_sha(p): p.name for p in _images(train_dir)}
    refs = {_sha(p): p.name for p in _images(references_dir)}
    if not train or not refs:
        raise TrainingError(f"{'training images' if not train else 'held-out references'} are missing")
    both = sorted(refs[h] for h in set(train) & set(refs))
    if both:
        raise TrainingError(f"held-out references are training images: {', '.join(both)}")


def judge(
    report: dict[str, Any], metric: str, min_score: float | None
) -> tuple[bool, bool, bool, float | None, list[str]]:
    """(passed, proof_passed, pending, score, reasons) for proof's ``EvalReport``.

    Passing: proof's verdict passed and isn't pending, ``metric``'s mean was scored, and it is at
    least ``min_score`` when one is set. ``likeness`` carries no weight in proof's verdict, so a
    likeness identity needs a ``min_score`` to pass.
    """
    verdict = report.get("verdict") or {}
    proof_passed = verdict.get("passed") is True
    pending = bool(verdict.get("pending"))
    means = (report.get("aggregate") or {}).get("means") or {}
    score = means.get(metric)
    score = float(score) if isinstance(score, (int, float)) else None
    reasons = [str(x) for x in verdict.get("reasons") or []]
    if not proof_passed:
        reasons.append("proof's verdict didn't pass")
    if pending:
        reasons.append("proof is waiting on a person's ratings (HITL)")
    if score is None:
        reasons.append(f"proof didn't score {metric} (no references, or its scorer isn't installed)")
    elif min_score is not None and score < min_score:
        reasons.append(f"{metric} {score:.3f} is under min_score {min_score}")
    if metric == "likeness" and min_score is None:
        reasons.append("likeness doesn't count in proof's verdict: the identity needs a min_score")
    passed = proof_passed and not pending and score is not None
    passed = passed and (min_score is None or score >= min_score) and not (metric == "likeness" and min_score is None)
    return passed, proof_passed, pending, score, reasons


# -- running a step ----------------------------------------------------------------------------------


@dataclass
class TrainingContext:
    """What a step needs. ``identity`` is the approved file being trained; ``save_identity`` saves
    a draft identity document as the next version and returns that version."""

    store: TrainingStore
    project: Project
    identity: Identity
    identity_dir: Path  # where the identity file is: its references are relative to it
    exports_dir: Path  # the exports volume, as mint sees it
    loras_dir: Path  # the LoRA directory darkroom and proof load from, as mint sees it
    renderer: Darklight | None = None
    curator: Curator | None = None
    lens: Lens | None = None
    forge: Forge | None = None
    proof: Proof | None = None
    save_identity: Callable[[dict[str, Any]], int] | None = None
    # references.ConsentStore: a real person's consent, checked again whenever references are used
    # (it can be revoked after approval). None: no records here, so such a reference is refused.
    consent: Any = None
    made_with: Callable[[Rendered], dict[str, str]] | None = None
    poll_seconds: float = 30.0
    poll_limit: float = 7 * 24 * 3600.0
    sleep: Callable[[float], None] = time.sleep

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.project.id, self.identity.id, self.identity.version)

    def record(self) -> TrainingRecord:
        r = self.store.load(*self.key)
        if r is None:
            raise TrainingError("no training record")
        return r

    def update(self, **fields: Any) -> TrainingRecord:
        return self.store.update(*self.key, **fields)


def run_step(step: str, ctx: TrainingContext, note: Callable[..., None]) -> dict[str, Any]:
    """Run ``step`` for ``ctx``; returns its outputs. ``note(sibling=…, inputs=…)`` records what is
    known as soon as it is, so a restart mid-step can find the sibling's run."""
    fn = {
        "candidates": _candidates,
        "select": _select,
        "caption": _caption,
        "train": _train,
        "evaluate": _evaluate,
        "pin": _pin,
    }[step]
    return fn(ctx, note)


def _need(client: Any, name: str) -> Any:
    if client is None:
        raise TrainingError(f"no {name} is configured")
    return client


def _candidates(ctx: TrainingContext, note: Callable[..., None]) -> dict[str, Any]:
    renderer = _need(ctx.renderer, "darkroom")
    ident, t = ctx.identity, _training(ctx.identity)
    r = ctx.record()
    finally_failed = {f.n for f in r.failed if f.final}
    todo = [n for n in range(1, t.candidates + 1) if n not in {c.n for c in r.candidates} | finally_failed]
    note(inputs={"model": t.model, "count": t.candidates, "rendering": todo})
    d = ctx.store.dir(*ctx.key)
    errors = []
    for n in todo:
        seed = candidate_seed(ctx.project.id, ident, n)
        try:
            rendered = render_requests(
                renderer, lambda s: candidate_request(ident, s), seed, t.max_attempts,
                label=f"{ident.id} v{ident.version} candidate {n}",
            )  # fmt: skip
        except RenderError as e:
            final = isinstance(e, RenderFailed)
            f = FailedCandidate(
                n=n, seed=seed, error=str(e), final=final, attempts=tuple(a.to_json() for a in e.attempts)
            )
            with ctx.store.lock:
                r = ctx.record()
                ctx.update(failed=tuple(sorted((*(x for x in r.failed if x.n != n), f), key=lambda x: x.n)))
            if not final:
                errors.append(f"candidate {n}: {e}")
            continue
        c = Candidate(
            n=n, seed=seed, file=candidate_file(ident, n), sha256=rendered.image_sha256, request=rendered.request,
            attempts=tuple(a.to_json() for a in rendered.attempts),
            made_with=ctx.made_with(rendered) if ctx.made_with else {},
        )  # fmt: skip
        (d / CANDIDATES).mkdir(parents=True, exist_ok=True)
        (d / c.file).write_bytes(rendered.image)
        with ctx.store.lock:
            r = ctx.record()
            ctx.update(
                candidates=tuple(sorted((*r.candidates, c), key=lambda x: x.n)),
                failed=tuple(x for x in r.failed if x.n != n),
            )
    r = ctx.record()
    if errors:
        raise TrainingError(f"{len(errors)} candidate(s) stopped on an error; start the step again: {errors[0]}")
    return {"rendered": len(r.candidates), "refused": len([f for f in r.failed if f.final])}


def _references_cleared(ctx: TrainingContext) -> None:
    """Refuse the step if any of the identity's references can't be used right now: an uncleared
    source, or a real person whose consent doesn't cover training today (revoked, expired...)."""
    if not ctx.identity.references:
        return
    from argus_mint.references import reference_problems  # argus-cortex: only where references are used

    found = reference_problems(ctx.identity, ctx.consent, at=datetime.now(UTC))
    if found:
        raise TrainingError("the identity's references can't be used now: " + "; ".join(found))


def _select(ctx: TrainingContext, note: Callable[..., None]) -> dict[str, Any]:
    curator = _need(ctx.curator, "curator")
    _references_cleared(ctx)  # before any reference reaches curator or proof's export
    ident, t = ctx.identity, _training(ctx.identity)
    if ctx.record().metric != "identity":
        raise TrainingError(
            "curator can select by consistency only for a face (face_source); a likeness identity's "
            "training set can't be selected yet"
        )
    r = ctx.record()
    if len(r.candidates) < t.train + t.held_out:
        raise TrainingError(f"{len(r.candidates)} candidates; {t.train} to train and {t.held_out} held out need more")
    d = ctx.store.dir(*ctx.key)
    folder = f"mint/{ctx.project.id}/{ident.id}-v{ident.version}"
    files = [(Path(c.file).name, (d / c.file).read_bytes()) for c in r.candidates]
    anchors: list[str] = []
    for i, ref in enumerate((x for x in ident.references if not x.held_out), 1):
        data = (ctx.identity_dir / ref.file).read_bytes()
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise TrainingError(f"reference {ref.file} differs from its recorded sha256")
        name = f"anchor-{i:02d}{Path(ref.file).suffix.lower() or '.png'}"
        files.append((name, data))
        anchors.append(name)
    note(inputs={"folder": folder, "candidates": len(r.candidates), "anchors": anchors})
    uploaded = curator.upload(folder, files)
    if uploaded.get("errors"):
        raise TrainingError(f"curator refused uploads: {uploaded['errors']}")
    scan = curator.scan({"folder": folder, "faces": {"enabled": True, "mivolo": True, "reference_paths": anchors}})
    scan_id = str(scan["scan_id"])
    note(sibling={"curator_scan": scan_id})
    if anchors and scan.get("reference_skipped"):
        raise TrainingError(f"curator couldn't use the identity's references: {scan['reference_skipped']}")
    face_source = {
        "single_face": True, "max_yaw": None, "max_pitch": None, "max_roll": None, "min_face_px": None,
        "min_face_sharpness": None, "min_eye_open": None, "max_mouth_open": None,
        "max_identity_distance": 0.4, "require_adult": True,
    }  # fmt: skip
    picked = curator.select(scan_id, {"face_source": face_source})
    names = {Path(c.file).name: c for c in r.candidates}
    kept = [row["rel_path"] for row in picked.get("rows") or [] if not row.get("keep_reason")]
    kept = [k for k in kept if k in names]  # never an anchor
    train, held = split_held_out(rank_kept(scan, kept), t.train, t.held_out)
    train_export, proof_export = export_names(*ctx.key)
    for name in (train_export, proof_export):  # mint's own directories, from an earlier try
        shutil.rmtree(ctx.exports_dir / name, ignore_errors=True)
    curator.export({"scan_id": scan_id, "selection": train, "dest": train_export, "preserve_structure": False})
    curator.export({"scan_id": scan_id, "selection": held, "dest": f"{proof_export}/references",
                    "preserve_structure": False})  # fmt: skip
    train_dir, proof_dir = ctx.exports_dir / train_export, ctx.exports_dir / proof_export
    manifest = train_dir / "manifest.jsonl"
    if not manifest.is_file():
        raise TrainingError(f"curator's training export has no manifest.jsonl at {manifest}")
    # proof's held-out check reads the export's training images: give it the training manifest.
    shutil.copyfile(manifest, proof_dir / "manifest.jsonl")
    for ref in (x for x in ident.references if x.held_out):  # the identity's own held-out references
        shutil.copyfile(ctx.identity_dir / ref.file, proof_dir / "references" / f"identity-{Path(ref.file).name}")
    check_held_out(train_dir, proof_dir / "references")
    sel = Selection(
        scan_id=scan_id, kept=len(kept), train=tuple(train), held_out=tuple(held), anchors=tuple(anchors),
        train_export=train_export, proof_export=proof_export,
        train_sha256=tuple(_sha(p) for p in _images(train_dir)),
        held_out_sha256=tuple(_sha(p) for p in _images(proof_dir / "references")),
    )  # fmt: skip
    ctx.update(selection=sel)
    return {"kept": len(kept), "train": len(train), "held_out": len(held), "train_export": train_export,
            "proof_export": proof_export}  # fmt: skip


def _selection(ctx: TrainingContext) -> Selection:
    sel = ctx.record().selection
    if sel is None:
        raise TrainingError("nothing is selected yet")
    return sel


def _caption(ctx: TrainingContext, note: Callable[..., None]) -> dict[str, Any]:
    lens = _need(ctx.lens, "lens")
    t = _training(ctx.identity)
    sel = _selection(ctx)
    train_dir = ctx.exports_dir / sel.train_export
    images = _images(train_dir)
    if len(images) != len(sel.train):
        raise TrainingError(f"the training export has {len(images)} images, not the {len(sel.train)} selected")
    form = {"target_style": t.caption_style, "target_backend": ctx.project.family, "trigger_word": ""}
    note(inputs={**form, "images": len(images)})
    out = lens.caption_batch([(p.name, p.read_bytes()) for p in images], form)
    results, errors = out.get("results") or {}, out.get("errors") or {}
    captions = []
    for p in images:
        res = results.get(p.name)
        text = ((res or {}).get("caption_variants") or {}).get("training")
        if not isinstance(text, str) or not text.strip():
            why = errors.get(p.name) or "no training caption"
            raise TrainingError(f"lens didn't caption {p.name}: {why}")
        body = text.strip()
        if body.split(",", 1)[0].strip() == t.trigger:
            body = body.split(",", 1)[1].strip() if "," in body else ""
        line = f"{t.trigger}, {body}" if body else t.trigger
        (train_dir / f"{p.stem}.txt").write_text(line + "\n", encoding="utf-8")
        captions.append(Caption(file=p.name, text=line))
    ctx.update(captions=tuple(captions))
    return {"captioned": len(captions)}


def _lora_names(ctx: TrainingContext) -> tuple[str, str]:
    """(forge's output_name, the LoRA's name in the shared LoRA directory)."""
    train_export, _ = export_names(*ctx.key)
    return train_export, f"mint/{train_export}.safetensors"


def _train(ctx: TrainingContext, note: Callable[..., None]) -> dict[str, Any]:
    forge = _need(ctx.forge, "forge")
    t = _training(ctx.identity)
    sel = _selection(ctx)
    check_held_out(ctx.exports_dir / sel.train_export, ctx.exports_dir / sel.proof_export / "references")
    if (forge.health() or {}).get("training") != "enabled":
        raise TrainingError("forge's training is disabled: it trains only on the trusted GPU host")
    output_name, lora_name = _lora_names(ctx)
    run_id = None
    earlier = [x for x in ctx.record().runs if x.step == "train" and x.sibling.get("forge_run")]
    if earlier:  # a run an earlier try started: follow it rather than train twice
        prev = earlier[-1].sibling["forge_run"]
        state = forge.status(prev)
        if state is not None and state.get("status") in ("running", "succeeded"):
            run_id = prev
    if run_id is None:
        config = {
            "export_dir": sel.train_export, "trainer": "kohya", "trigger": t.trigger, "output_name": output_name,
            "collect_captions": False, "dry_run": False,
            **({"base_model": t.base_model} if t.base_model else {}),
        }  # fmt: skip
        note(inputs=config)
        forged = forge.config(config)
        if any("dry" in str(w).lower() and "run" in str(w).lower() for w in forged.get("warnings") or []):
            raise TrainingError(f"forge only previewed the config: {forged.get('warnings')}")
        started = forge.run({"export_dir": sel.train_export, "trainer": "kohya"})
        run_id = str(started["run_id"])
    note(sibling={"forge_run": run_id})
    waited = 0.0
    while True:
        state = forge.status(run_id)
        if state is None:
            raise TrainingError(f"forge no longer knows run {run_id} (it restarted); start the step again")
        status = state.get("status")
        if status == "succeeded":
            break
        if status in ("failed", "cancelled"):
            raise TrainingError(f"forge run {run_id} {status}: {state.get('message') or state.get('returncode')}")
        if waited >= ctx.poll_limit:
            raise TrainingError(f"forge run {run_id} is still {status} after {waited:.0f}s")
        ctx.sleep(ctx.poll_seconds)
        waited += ctx.poll_seconds
    out = ctx.exports_dir / sel.train_export / "forge" / "kohya" / "output" / f"{output_name}.safetensors"
    if not out.is_file():
        raise TrainingError(f"forge run {run_id} succeeded but left no {out.name} in its output")
    sha = _sha(out)
    dest = ctx.loras_dir / lora_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    shutil.copyfile(out, tmp)
    tmp.replace(dest)
    if _sha(dest) != sha:
        raise TrainingError(f"the LoRA copied to {dest} differs from forge's")
    lora = TrainedLora(name=lora_name, sha256=sha, forge_run=run_id, output_name=output_name, trigger=t.trigger)
    ctx.update(lora=lora)
    return {"lora": lora_name, "sha256": sha}


def _evaluate(ctx: TrainingContext, note: Callable[..., None]) -> dict[str, Any]:
    proof = _need(ctx.proof, "proof")
    _references_cleared(ctx)  # proof scores against them: consent may have changed since select
    ident, t = ctx.identity, _training(ctx.identity)
    sel = _selection(ctx)
    lora = ctx.record().lora
    if lora is None:
        raise TrainingError("no LoRA is trained yet")
    if not (ctx.loras_dir / lora.name).is_file() or _sha(ctx.loras_dir / lora.name) != lora.sha256:
        raise TrainingError(f"{lora.name} in the LoRA directory isn't the one forge trained")
    check_held_out(ctx.exports_dir / sel.train_export, ctx.exports_dir / sel.proof_export / "references")
    report = None
    earlier = [x for x in ctx.record().runs if x.step == "evaluate" and x.sibling.get("proof_run")]
    if earlier:  # a run an earlier try started may have finished in proof meanwhile
        run_id = earlier[-1].sibling["proof_run"]
        report = proof.report(run_id)
    if report is None:
        run_id = f"{sel.train_export}-{int(time.time())}"
        body: dict[str, Any] = {
            "run_id": run_id, "lora": lora.name, "lora_weight": 1.0, "base_checkpoint": t.model,
            "export": sel.proof_export, "prompt": f"{t.trigger}, {ident.prompt}", "negative_prompt": ident.negative,
            "seeds": [candidate_seed(ctx.project.id, ident, 1000 + i) for i in range(t.proof_seeds)],
            "steps": t.sampling.steps, "cfg": t.sampling.cfg, "sampler": t.sampling.sampler,
            "scheduler": t.sampling.scheduler, "clip_skip": t.sampling.clip_skip, "width": t.width, "height": t.height,
            **({"guidance": t.sampling.guidance} if t.sampling.guidance is not None else {}),
        }  # fmt: skip
        note(inputs=body, sibling={"proof_run": run_id})
        last = proof.run(body)
        if last.get("type") != "complete":
            raise TrainingError(f"proof run {run_id} stopped: {last.get('message') or last}")
        report = proof.report(run_id)
        if report is None:
            raise TrainingError(f"proof run {run_id} completed but has no report")
    passed, proof_passed, pending, score, reasons = judge(report, ctx.record().metric, ident.min_score)
    ev = Evaluation(
        proof_run=run_id, metric=ctx.record().metric, score=score, min_score=ident.min_score,
        proof_passed=proof_passed, pending=pending, passed=passed, reasons=tuple(reasons),
    )  # fmt: skip
    ctx.update(evaluation=ev)
    return {"proof_run": run_id, "passed": passed, "pending": pending, "score": score}


def _pin(ctx: TrainingContext, note: Callable[..., None]) -> dict[str, Any]:
    proof = _need(ctx.proof, "proof")
    if ctx.save_identity is None:
        raise TrainingError("identity files can't be saved here")
    r = ctx.record()
    ident = ctx.identity
    if r.evaluation is None or r.lora is None or r.selection is None:
        raise TrainingError("nothing is evaluated yet")
    run_id = r.evaluation.proof_run
    note(inputs={"proof_run": run_id}, sibling={"proof_run": run_id})
    report = proof.report(run_id)  # again: ratings made in proof since the evaluation count
    if report is None:
        raise TrainingError(f"proof has no report for {run_id}")
    passed, proof_passed, pending, score, reasons = judge(report, r.metric, ident.min_score)
    ctx.update(evaluation=r.evaluation.model_copy(update={
        "score": score, "proof_passed": proof_passed, "pending": pending, "passed": passed, "reasons": tuple(reasons),
        "min_score": ident.min_score,
    }))  # fmt: skip
    if not passed:
        raise TrainingError(f"proof run {run_id} doesn't pass: {'; '.join(reasons)}")
    manifest = proof.manifest(run_id)
    if manifest.get("run_id") != run_id:
        raise TrainingError(f"proof's manifest is for run {manifest.get('run_id')!r}, not {run_id}")
    loras = [x for x in manifest.get("loras") or [] if isinstance(x, dict)]
    if len(loras) != 1 or loras[0].get("name") != r.lora.name or loras[0].get("sha256") != r.lora.sha256:
        raise TrainingError(f"proof's run {run_id} evaluated another LoRA than {r.lora.name} ({r.lora.sha256[:12]}…)")
    doc = ident.model_dump(mode="json", exclude={"status", "approved_by", "approved_at", "guard", "version"})
    doc |= {
        "lora": {"name": r.lora.name, "weight": float(loras[0].get("weight", 1.0)), "trigger_words": [r.lora.trigger]},
        "proof_run": run_id,
        "proof_export": r.selection.proof_export,
    }
    version = ctx.save_identity(doc)
    pinned = Pinned(
        identity_version=version, lora=r.lora.name, sha256=r.lora.sha256, proof_run=run_id,
        proof_export=r.selection.proof_export,
    )  # fmt: skip
    ctx.update(pinned=pinned)
    return {"identity_version": version, "lora": r.lora.name, "proof_run": run_id}


def vouches(records: list[TrainingRecord], project: str, identity: Identity) -> str | None:
    """Why ``identity``'s pinned LoRA isn't one a passing training run pinned, or None when it is.
    An identity without a LoRA needs nothing."""
    if identity.lora is None:
        return None
    for r in records:
        p = r.pinned
        if (
            r.project == project
            and r.identity == identity.id
            and p is not None
            and r.evaluation is not None
            and r.evaluation.passed
            and (p.identity_version, p.lora, p.proof_run, p.proof_export)
            == (identity.version, identity.lora.name, identity.proof_run, identity.proof_export)
        ):
            return None
    return (
        f"no passing training run pinned {identity.lora.name} with proof run {identity.proof_run} "
        f"into {identity.id} v{identity.version}"
    )
