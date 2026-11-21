"""HTTP routes for training an embodied identity (``training.py``), one step at a time.

=================================================================================  ===============================
``GET  /projects/{id}/identities/{identity}/{version}/training``                   the record, each step's status
``POST /projects/{id}/identities/{identity}/{version}/training/{step}``            start one step (a job)
``GET  /projects/{id}/identities/{identity}/{version}/training/candidates/{n}``    a candidate's PNG
=================================================================================  ===============================

The step is in the path, so each request names the one step the moderator means to run: no
request starts two, and no step starts the next. Steps are ``candidates``, ``select``,
``caption``, ``train``, ``evaluate`` and ``pin``. Jobs run one at a time on one thread, like
background jobs; a job a restart cut off reads as failed.
"""

from __future__ import annotations

import queue
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response

import argus_mint
from argus_mint.darkroom import DarkroomError
from argus_mint.projects import Identity, ProjectBundle, ProjectError, file_strings, fresh_verdicts, load_bundle
from argus_mint.references import ConsentStore
from argus_mint.render import Rendered
from argus_mint.spoilers import SpoilerList
from argus_mint.training import (
    STEPS,
    StepRun,
    TrainingContext,
    TrainingError,
    TrainingStore,
    refusal,
    run_step,
    step_state,
    vouches,
)
from argus_mint.wire import IdentityTrainingView

log = structlog.get_logger(__name__)

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass
class _Job:
    run: StepRun
    ctx: TrainingContext


@dataclass
class TrainingRunner:
    """Training steps, one at a time on one thread. ``live``: the jobs this process still owns."""

    store: TrainingStore
    jobs: queue.Queue[_Job] = field(default_factory=queue.Queue)
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    live: set[str] = field(default_factory=set)

    def submit(self, ctx: TrainingContext, step: str) -> StepRun:
        run = StepRun(step=step, job=secrets.token_hex(8), status="queued", started_at=datetime.now(UTC))  # type: ignore[arg-type]
        with self.lock:
            self.live.add(run.job)
            self.store.set_run(*ctx.key, run)
            self.jobs.put(_Job(run, ctx))
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._work, name="argus-mint-training", daemon=True)
                self.thread.start()
        return run

    def _work(self) -> None:
        while True:
            try:
                job = self.jobs.get(timeout=5)
            except queue.Empty:
                with self.lock:
                    if self.jobs.empty():
                        self.thread = None
                        return
                continue
            try:
                self._run(job)
            except Exception as e:  # the record can't be written: keep working through the queue
                log.warning("training_job_crashed", job=job.run.job, error=str(e))
            finally:
                self.live.discard(job.run.job)

    def _run(self, job: _Job) -> None:
        ctx = job.ctx
        run = job.run.model_copy(update={"status": "running"})
        self.store.set_run(*ctx.key, run)
        box = {"run": run}

        def note(**fields: Any) -> None:
            cur = box["run"]
            update = {k: {**getattr(cur, k), **v} for k, v in fields.items()}
            box["run"] = cur.model_copy(update=update)
            self.store.set_run(*ctx.key, box["run"])

        try:
            outputs = run_step(run.step, ctx, note)
        except Exception as e:  # a step's failure is recorded, not raised
            detail = getattr(e, "detail", None) or str(e)
            log.warning("training_step_failed", step=run.step, job=run.job, error=str(detail))
            self.store.set_run(*ctx.key, box["run"].model_copy(
                update={"status": "failed", "error": str(detail), "finished_at": datetime.now(UTC)}
            ))  # fmt: skip
            return
        self.store.set_run(*ctx.key, box["run"].model_copy(
            update={"status": "done", "outputs": outputs, "finished_at": datetime.now(UTC)}
        ))  # fmt: skip


@dataclass
class TrainingClients:
    """The siblings, and where the shared volumes are as mint sees them. Any may be None (unset)."""

    curator: Any = None
    lens: Any = None
    forge: Any = None
    proof: Any = None
    exports_dir: Path | None = None
    loras_dir: Path | None = None
    poll_seconds: float = 30.0
    sleep: Callable[[float], None] | None = None

    def missing(self) -> list[str]:
        named = {
            "curator (ARGUS_MINT_CURATOR_URL)": self.curator,
            "lens (ARGUS_MINT_LENS_URL)": self.lens,
            "forge (ARGUS_MINT_FORGE_URL)": self.forge,
            "proof (ARGUS_MINT_PROOF_URL)": self.proof,
            "the shared exports directory (ARGUS_MINT_TRAINING_EXPORTS_DIR)": self.exports_dir,
            "the shared LoRA directory (ARGUS_MINT_LORAS_DIR)": self.loras_dir,
        }
        return [k for k, v in named.items() if v is None]


def add_training_routes(
    app: FastAPI,
    *,
    projects_dir: Path,
    training_dir: Path,
    clients: TrainingClients,
    spoilers: SpoilerList,
    darkroom: Any,
    require_token: Callable[[str | None], None],
    save_file: Callable[[str, str, str, int, dict[str, Any]], Any],
    consent_dir: Path | None = None,
) -> tuple[TrainingRunner, Callable[[str, Identity], str | None]]:
    """Add the routes. Returns the runner and ``vouch(pid, identity)`` for the project routes'
    approval check (an identity's LoRA is approved only as a passing training run pinned it)."""
    store = TrainingStore(training_dir)
    runner = TrainingRunner(store)
    starting = threading.Lock()

    def bundle(pid: str) -> ProjectBundle:
        if not _ID.fullmatch(pid) or not (projects_dir / pid / "project.yaml").is_file():
            raise HTTPException(404, f"no project {pid!r}")
        try:
            return load_bundle(projects_dir / pid)
        except ProjectError as e:
            raise HTTPException(422, str(e)) from e

    def identity_of(b: ProjectBundle, iid: str, version: int) -> tuple[Identity, Path]:
        found = b.identities.get((iid, version))
        if found is None:
            raise HTTPException(404, f"no identity {iid} v{version} in project {b.id}")
        return found

    def view(pid: str, iid: str, version: int) -> IdentityTrainingView:
        # Which jobs are live, asked before the record is read: a job that finishes between the two
        # (its last save, then leaving `live`) would otherwise read as running, not live, so failed.
        live = set(runner.live)
        try:
            r = store.load(pid, iid, version)
        except TrainingError as e:
            raise HTTPException(404 if "not an identity" in str(e) else 500, str(e)) from e
        steps = step_state(r, live)
        nxt = next((s for s in STEPS if refusal(r, s, live) is None), None)
        return IdentityTrainingView(
            project=pid,
            identity=iid,
            version=version,
            steps=steps,
            next=nxt,
            record=r,  # type: ignore[arg-type]
        )

    def made_with(rendered: Rendered) -> dict[str, str]:
        out = {"argus-mint": argus_mint.__version__}
        if rendered.renderer_version:
            out["darkroom"] = rendered.renderer_version
        return out

    def vouch(pid: str, identity: Identity) -> str | None:
        return vouches([r for r in store.all() if r.project == pid], pid, identity)

    @app.get("/projects/{pid}/identities/{iid}/{version}/training")
    def get_training(pid: str, iid: str, version: int) -> IdentityTrainingView:
        identity_of(bundle(pid), iid, version)
        return view(pid, iid, version)

    @app.get("/projects/{pid}/identities/{iid}/{version}/training/candidates/{n}")
    def candidate_image(pid: str, iid: str, version: int, n: int) -> Response:
        identity_of(bundle(pid), iid, version)
        try:
            r = store.load(pid, iid, version)
        except TrainingError as e:
            raise HTTPException(404 if "not an identity" in str(e) else 500, str(e)) from e
        if r is None:
            raise HTTPException(404, f"{iid} v{version} has no training record")
        try:
            return Response(store.candidate_image(r, n), media_type="image/png")
        except TrainingError as e:
            raise HTTPException(404 if "no candidate" in str(e) else 500, str(e)) from e

    @app.post("/projects/{pid}/identities/{iid}/{version}/training/{step}", status_code=202)
    def start_step(
        pid: str, iid: str, version: int, step: str, authorization: Annotated[str | None, Header()] = None
    ) -> IdentityTrainingView:
        """Start ``step``: refused unless the step before it is done and nothing else of it runs."""
        require_token(authorization)
        if step not in STEPS:
            raise HTTPException(404, f"no training step {step!r} (steps: {', '.join(STEPS)})")
        missing = clients.missing() + ([] if darkroom is not None else ["darkroom (ARGUS_MINT_DARKROOM_URL)"])
        if missing:
            raise HTTPException(503, "identity training isn't set up here: no " + ", ".join(missing))
        b = bundle(pid)
        ident, path = identity_of(b, iid, version)
        for kind, obj in (("project", b.project), ("identity", ident)):
            if obj.status != "approved":
                raise HTTPException(
                    409, f"{kind} {obj.id} v{obj.version} is {obj.status}; only approved identities train"
                )
        if ident.presence != "embodied":
            raise HTTPException(409, f"identity {iid} is a viewpoint: nothing is painted in, so nothing is trained")
        if ident.training is None or ident.check_metric is None:
            raise HTTPException(409, f"identity {iid} v{version} needs a `training` block and a `check_metric`")
        if ident.lora is not None:
            raise HTTPException(409, f"identity {iid} v{version} already has a LoRA pinned")
        hits = spoilers.hits(*file_strings(ident.model_dump(mode="json")))
        if hits:
            raise HTTPException(409, "held by spoiler rule " + ", ".join(map(str, hits)))
        fresh, unjudged = fresh_verdicts(ident, b.project.policy)
        failed = [f for f, v in fresh.items() if not v.passed]
        if unjudged or failed:
            raise HTTPException(409, f"identity {iid} v{version}'s prompt has no fresh passing SingGuard verdict")
        if step == "candidates":
            check = getattr(darkroom, "check_render", None)
            if check is not None:
                try:
                    refused = check(ident.training.model, 0)
                except DarkroomError as e:
                    raise HTTPException(502, str(e)) from e
                if refused:
                    raise HTTPException(409, "darkroom can't render the candidates: " + "; ".join(refused))

        def save_identity(doc: dict[str, Any]) -> int:
            nb = bundle(pid)
            v = max(ver for (i, ver) in nb.identities if i == iid) + 1
            save_file(pid, "identity", iid, v, doc)
            return v

        assert clients.exports_dir is not None and clients.loras_dir is not None
        ctx = TrainingContext(
            store=store, project=b.project, identity=ident, identity_dir=path.parent,
            exports_dir=clients.exports_dir, loras_dir=clients.loras_dir,
            renderer=darkroom.batch() if hasattr(darkroom, "batch") else darkroom,
            curator=clients.curator, lens=clients.lens, forge=clients.forge, proof=clients.proof,
            save_identity=save_identity, made_with=made_with, poll_seconds=clients.poll_seconds,
            consent=ConsentStore(consent_dir) if consent_dir is not None else None,
            **({"sleep": clients.sleep} if clients.sleep is not None else {}),
        )  # fmt: skip
        with starting:
            live = set(runner.live)  # before the record is read (see view)
            with store.lock:
                try:
                    r = store.start(pid, ident)
                except TrainingError as e:
                    raise HTTPException(409, str(e)) from e
                if store.load(pid, iid, version) is None:
                    store.save(r)
            why = refusal(r, step, live)
            if why:
                raise HTTPException(409, why)
            runner.submit(ctx, step)
        return view(pid, iid, version)

    return runner, vouch
