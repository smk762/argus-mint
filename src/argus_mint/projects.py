"""Teaser projects: the reviewed files behind image + quote teasers.

A project lives in one directory::

    <projects>/<project-id>/
      project.yaml          # title, attribution, frames, SingGuard policy, checkpoint family
      identities/*.yaml     # who appears (or whose view it is), and how they keep their look
      scenes/*.yaml         # the backgrounds: prompt, sampling, how many to render, where a figure goes
      lines/*.yaml          # the quotes, verbatim, with the published wording and their source
      teasers/*.json        # which line goes with which identity, scene and frame, and its alt text
      frames/*.yaml         # optional: the project's own frames, ids ``<project>/<name>`` (argus_mint.frames)

Every file is versioned and reviewed like a template (``draft -> approved -> retired``,
``catalogue.record_decision``), so only approved files make teasers (hard rule 2). A scene's
prompt is fixed text written by the author and reviewed before anything renders from it; a
teaser never takes a prompt (hard rule 1). The teaser links are one JSON file per version
(``teasers/<project>-v<n>.json``), reviewed like the rest; each image they lead to is reviewed
again as a draft, by the moderator and the owner.

Each file keeps SingGuard's verdicts on its own texts in a ``guard`` block: the field, the
text's sha256, the policy and the outcome. Like ``status``, ``guard`` is left out of the file's
hash. A verdict counts only while the text and the project's policy are what it judged
(:func:`fresh_verdicts`), so a stored pass survives a restart but never an edit.

Pure: reading the files and checking them, no network. :func:`problems` lists everything that
is wrong with a project at once, for the author, rather than stopping at the first.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from argus_mint._codec import canonical, keccak256
from argus_mint.frames import FRAME_ID_PATTERN
from argus_mint.templates import LoraRef, Sampling, family

__all__ = [
    "GuardVerdict",
    "Identity",
    "IdentityTraining",
    "Line",
    "Project",
    "ProjectBundle",
    "ProjectError",
    "Reference",
    "FrameLink",
    "Ref",
    "Scene",
    "TeaserLink",
    "TeaserLinks",
    "REVIEW_KEYS",
    "fresh_verdicts",
    "file_strings",
    "load_bundle",
    "load_identity",
    "load_line",
    "load_project",
    "load_scene",
    "load_teasers",
    "problems",
    "teaser_problems",
    "text_sha256",
]

_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
_TEASER_ID = r"^T[1-9][0-9]{0,4}$"
Status = Literal["draft", "approved", "retired"]
Voice = str  # free label chosen by the author ("synth", "narrator"...); lines and identities must agree


class ProjectError(ValueError):
    """A project file is invalid."""


class Ref(BaseModel):
    """A reviewed file named by id and version, never by path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=_ID_PATTERN)
    version: int = Field(ge=1)

    @property
    def key(self) -> tuple[str, int]:
        return (self.id, self.version)


class FrameLink(Ref):
    """A frame by id and version: a global frame's id, or one of the project's own, ``<project>/<name>``.

    It dumps exactly as a :class:`Ref` does, so files written before project frames hash as before."""

    id: str = Field(pattern=FRAME_ID_PATTERN)

    @property
    def project(self) -> str | None:
        return self.id.split("/", 1)[0] if "/" in self.id else None


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class GuardVerdict(BaseModel):
    """SingGuard's verdict on one text of a file, as stored in the file's ``guard`` block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str = Field(min_length=1, max_length=64)  # e.g. "prompt", or "T1.alt" in teaser links
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # of the text judged
    policy: str = Field(min_length=1, max_length=64)
    passed: bool
    detail: str = Field(default="", max_length=500)  # the rule or reason when it didn't pass
    checked_at: datetime


# Written by the server, never by a client: a saved document may not carry them.
REVIEW_KEYS = ("status", "approved_by", "approved_at", "guard")


class _Reviewed(BaseModel):
    """Fields every reviewed project file has, with the approval rule templates use."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=_ID_PATTERN)
    version: int = Field(ge=1)
    status: Status
    approved_by: str | None = None
    approved_at: date | None = None
    guard: tuple[GuardVerdict, ...] = ()  # SingGuard's verdicts on this file's texts; not hashed

    @model_validator(mode="after")
    def _approval_recorded(self) -> _Reviewed:
        if self.status != "draft" and (not self.approved_by or self.approved_at is None):
            raise ValueError(f"status {self.status!r} requires approved_by and approved_at (approval is a human step)")
        return self

    @property
    def key(self) -> tuple[str, int]:
        return (self.id, self.version)

    def hash(self) -> bytes:
        """keccak256 of the file's canonical JSON, excluding ``status`` (as for templates) and ``guard``."""
        return keccak256(canonical(self.model_dump(mode="json", exclude={"status", "guard"})))

    def guarded_texts(self) -> list[tuple[str, str]]:
        """(field, text) for every text SingGuard reads in this file (negative prompts are left out:
        they name what to avoid)."""
        return [(f, t) for f in self.GUARDED if isinstance(t := getattr(self, f), str) and t.strip()]

    GUARDED: ClassVar[tuple[str, ...]] = ()


class Project(_Reviewed):
    """The project's settings: what every teaser in it shares."""

    title: str = Field(min_length=1, max_length=120)  # the working title; what is posted becomes the public name
    attribution: str | None = Field(default=None, max_length=120)  # a fixed line under each quote; None: unsigned
    generated_label: bool = True  # draw "generated image" (the frame's `label` slot) on each teaser
    frames: tuple[FrameLink, ...] = ()  # frames teasers may use (one per aspect, or one per voice)
    policy: str = Field(default="default", min_length=1, max_length=64)  # SingGuard policy for this project's text
    family: str = Field(default="sdxl", min_length=1, max_length=32)  # checkpoint family (inpainting is SDXL-only)
    style_lora: LoraRef | None = None  # the project's look, trained once enough is approved

    GUARDED = ("title", "attribution")


class Reference(BaseModel):
    """An image an identity is trained or checked against, with where it came from.

    ``provenance`` is an argus-cortex ``Provenance`` record (``kind``, ``source``, ``license``,
    and for a real person ``subject`` and ``release_sha256``), kept as JSON here so this module
    stays pure. The server checks it with cortex's own models (:mod:`argus_mint.references`):
    only cleared kinds (``stock``, ``synthetic``, ``consented``) may be approved, since teasers
    are public, and a ``consented`` one needs a valid consent record that binds this image.
    An image mint rendered itself is ``synthetic``, with a ``generator`` (model, seed, prompt).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = Field(min_length=1)  # relative to the identity file
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: dict[str, Any]  # argus-cortex Provenance JSON
    held_out: bool = False  # kept out of training, so proof can score against it

    @model_validator(mode="after")
    def _has_a_kind(self) -> Reference:
        if not isinstance(self.provenance.get("kind"), str):
            raise ValueError(f"reference {self.file}: provenance needs a `kind` (argus-cortex Provenance)")
        return self


_TRIGGER = r"^[a-z][a-z0-9_]{2,31}$"


class IdentityTraining(BaseModel):
    """How an embodied identity's LoRA is trained (``training.py``): reviewed with the identity.

    ``model``/``sampling``/``width``/``height`` render the candidates through darkroom and the
    proof run's images (one checkpoint, named as darkroom and proof both load it); ``base_model``
    is the same checkpoint as argus-forge names it (its default when unset). Of ``candidates``
    renders, curator's selection gives ``train`` training images and ``held_out`` references,
    never the same image twice. ``trigger`` goes first in every caption and in the pinned LoRA's
    trigger words.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)  # darkroom (and proof) checkpoint
    base_model: str | None = Field(default=None, min_length=1)  # forge's name for it; None: forge's default
    sampling: Sampling
    width: int = Field(default=1024, gt=0, le=4096)
    height: int = Field(default=1024, gt=0, le=4096)
    candidates: int = Field(default=60, ge=8, le=200)
    train: int = Field(default=30, ge=4, le=150)
    held_out: int = Field(default=8, ge=2, le=50)
    trigger: str = Field(pattern=_TRIGGER)
    caption_style: Literal["photo", "anime"] = "photo"  # lens's target_style
    max_attempts: int = Field(default=4, ge=1, le=16)
    proof_seeds: int = Field(default=8, ge=1, le=64)

    @model_validator(mode="after")
    def _counts(self) -> IdentityTraining:
        if self.train + self.held_out > self.candidates:
            raise ValueError(
                f"train ({self.train}) and held_out ({self.held_out}) need more than {self.candidates} candidates"
            )
        return self


class Identity(_Reviewed):
    """Someone a teaser speaks for.

    ``embodied``: they appear in the image, painted in from ``prompt`` and kept consistent by
    ``lora`` once one has passed proof. ``viewpoint``: the image is what they see; ``frame``
    (a reviewed overlay, such as a security feed) is what makes it theirs, and nothing is
    painted in.

    ``lora``, ``proof_run`` and ``proof_export`` are pinned together, from a training run that
    passed (``training.py``): the LoRA, proof's run, and the export holding the held-out
    references proof scored against (a directory name under proof's ``ARGUS_PROOF_EXPORTS_DIR``).
    ``check_metric`` is the proof metric this identity is checked on (``identity``: ArcFace, for
    a face; ``likeness``: DINOv2, for anything else) and ``min_score`` its threshold, unset
    until the pilot sets one.
    """

    project: str = Field(pattern=_ID_PATTERN)
    voice: Voice = Field(min_length=1, max_length=32)
    presence: Literal["embodied", "viewpoint"]
    prompt: str = Field(default="", max_length=2000)  # appearance, for an embodied identity
    negative: str = Field(default="", max_length=2000)
    frame: FrameLink | None = None  # the overlay of a viewpoint identity
    lora: LoraRef | None = None  # pinned from proof's manifest once a training run passes
    proof_run: str | None = Field(default=None, min_length=1, max_length=128)  # the run that passed it
    proof_export: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    check_metric: Literal["identity", "likeness"] | None = None
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    training: IdentityTraining | None = None
    references: tuple[Reference, ...] = ()

    GUARDED = ("prompt",)
    # Added after identities were first written: left out of the hash while unset, so an
    # identity's hash (and every teaser draft that records it) is what it was.
    UNHASHED_WHEN_UNSET: ClassVar[tuple[str, ...]] = ("proof_export", "check_metric", "min_score", "training")

    def hash(self) -> bytes:
        unset = {f for f in self.UNHASHED_WHEN_UNSET if getattr(self, f) is None}
        return keccak256(canonical(self.model_dump(mode="json", exclude={"status", "guard", *unset})))

    @model_validator(mode="after")
    def _presence_fits(self) -> Identity:
        if "{" in self.prompt or "}" in self.prompt:
            raise ValueError("an identity's prompt is fixed text: no placeholders or braces")
        if self.presence == "embodied":
            if not self.prompt.strip():
                raise ValueError("an embodied identity needs a prompt describing them")
            if self.frame is not None:
                raise ValueError("an embodied identity has no frame of its own (the teaser names one)")
        else:
            if self.frame is None:
                raise ValueError("a viewpoint identity needs its frame")
            if self.lora is not None:
                raise ValueError("a viewpoint identity isn't painted in, so it takes no LoRA")
            if self.training is not None:
                raise ValueError("a viewpoint identity isn't painted in, so nothing is trained for it")
        if len({self.lora is None, self.proof_run is None, self.proof_export is None}) != 1:
            raise ValueError("a LoRA, the proof run that passed it and proof's export (its references) go together")
        files = [r.file for r in self.references]
        if len(set(files)) != len(files):
            raise ValueError("a reference is listed twice")
        return self


class Area(BaseModel):
    """Where in a background a figure may be painted, in the render's pixels."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class Scene(_Reviewed):
    """A setting. Its backgrounds are rendered first, a few each, and reviewed before any teaser uses one."""

    project: str = Field(pattern=_ID_PATTERN)
    model: str = Field(min_length=1)  # darkroom checkpoint
    sampling: Sampling
    width: int = Field(gt=0, le=8192)
    height: int = Field(gt=0, le=8192)
    prompt: str = Field(min_length=1, max_length=2000)
    negative: str = Field(default="", max_length=2000)
    backgrounds: int = Field(default=3, ge=1, le=12)  # how many to render for review
    character_area: Area | None = None  # None: no figure is painted into this scene
    max_attempts: int = Field(default=4, ge=1, le=16)

    GUARDED = ("prompt",)

    @model_validator(mode="after")
    def _fits(self) -> Scene:
        if any(c in self.prompt + self.negative for c in "{}"):
            raise ValueError("a scene's prompt is fixed text: no placeholders or braces")
        a = self.character_area
        if a is not None and (a.x + a.width > self.width or a.y + a.height > self.height):
            raise ValueError(
                f"character_area {a.x},{a.y} {a.width}x{a.height} falls outside {self.width}x{self.height}"
            )
        return self


class Line(_Reviewed):
    """A quote, as the author wrote it.

    ``text`` is verbatim from the source and never edited; ``published`` is what goes on the
    image (typos fixed, nothing else), and defaults to ``text``. No model ever writes or sees it.
    """

    project: str = Field(pattern=_ID_PATTERN)
    voice: Voice = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1, max_length=600)
    published: str | None = Field(default=None, min_length=1, max_length=600)
    source: str = Field(min_length=1, max_length=200)  # where it was written, e.g. a scene file

    GUARDED = ("text", "published")

    @property
    def shown(self) -> str:
        return self.published or self.text


class TeaserLink(BaseModel):
    """One teaser: a reviewed line, identity, scene and frame, and the image's alt text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line: Ref
    identity: Ref
    scene: Ref
    frame: FrameLink
    alt: str = Field(min_length=1, max_length=1000)  # posts refuse an image without it
    background: str | None = Field(default=None, min_length=1, max_length=64)  # the chosen render, once there is one


class TeaserLinks(_Reviewed):
    """``teasers/<project>-v<n>.json``: the project's teasers by number. Its id is the project's."""

    project: str = Field(pattern=_ID_PATTERN)
    teasers: dict[str, TeaserLink] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _numbers(self) -> TeaserLinks:
        if self.id != self.project:
            raise ValueError(f"teaser links take their project's id ({self.project}), not {self.id!r}")
        bad = [k for k in self.teasers if not re.fullmatch(_TEASER_ID, k)]
        if bad:
            raise ValueError(f"teaser numbers are T1, T2…: {bad}")
        return self

    def guarded_texts(self) -> list[tuple[str, str]]:
        return [(f"{n}.alt", t.alt) for n, t in self.teasers.items()]


def fresh_verdicts(obj: _Reviewed, policy: str) -> tuple[dict[str, GuardVerdict], list[tuple[str, str]]]:
    """The file's stored verdicts that still hold, by field, and the (field, text) pairs that have
    none: a verdict holds while the text and the policy are the ones it judged."""
    stored = {(v.field, v.sha256, v.policy): v for v in obj.guard}
    fresh: dict[str, GuardVerdict] = {}
    missing: list[tuple[str, str]] = []
    for f, text in obj.guarded_texts():
        v = stored.get((f, text_sha256(text), policy))
        if v is None:
            missing.append((f, text))
        else:
            fresh[f] = v
    return fresh, missing


_M = TypeVar("_M", bound=BaseModel)
_R = TypeVar("_R", bound=_Reviewed)


# -- loading --------------------------------------------------------------------------------------


def _load_yaml(path: Path, model: type[_M]) -> _M:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ProjectError(f"{path}: not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ProjectError(f"{path}: expected a mapping at the top level")
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise ProjectError(f"{path}: {e}") from e


def load_project(path: Path) -> Project:
    return _load_yaml(path, Project)


def load_identity(path: Path) -> Identity:
    return _load_yaml(path, Identity)


def load_scene(path: Path) -> Scene:
    return _load_yaml(path, Scene)


def load_line(path: Path) -> Line:
    return _load_yaml(path, Line)


def load_teasers(path: Path) -> TeaserLinks:
    """One version of a project's teaser links (JSON)."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ProjectError(f"{path}: not valid JSON: {e}") from e
    try:
        return TeaserLinks.model_validate(raw)
    except ValidationError as e:
        raise ProjectError(f"{path}: {e}") from e


@dataclass
class ProjectBundle:
    """Every file of one project, any status, keyed by (id, version), with each file's path."""

    root: Path
    project: Project
    identities: dict[tuple[str, int], tuple[Identity, Path]] = field(default_factory=dict)
    scenes: dict[tuple[str, int], tuple[Scene, Path]] = field(default_factory=dict)
    lines: dict[tuple[str, int], tuple[Line, Path]] = field(default_factory=dict)
    teasers: dict[tuple[str, int], tuple[TeaserLinks, Path]] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.root.name

    def current_teasers(self) -> TeaserLinks | None:
        """The newest approved version of the teaser links: the one teasers are made from."""
        approved = [t for t, _ in self.teasers.values() if t.status == "approved"]
        return max(approved, key=lambda t: t.version, default=None)


def _yaml_files(root: Path) -> list[Path]:
    return sorted([*root.glob("*.yaml"), *root.glob("*.yml")]) if root.is_dir() else []


def _collect(files: list[Path], loader: Callable[[Path], _R], kind: str) -> dict[tuple[str, int], tuple[_R, Path]]:
    out: dict[tuple[str, int], tuple[_R, Path]] = {}
    for f in files:
        obj = loader(f)
        if obj.key in out:
            raise ProjectError(f"{f}: duplicate {kind} {obj.id} v{obj.version} (also in {out[obj.key][1]})")
        out[obj.key] = (obj, f)
    return out


def load_bundle(root: Path) -> ProjectBundle:
    """Load one project directory. Raises :class:`ProjectError` on a file that doesn't validate."""
    root = Path(root)
    if not (root / "project.yaml").is_file():
        raise ProjectError(f"{root}: no project.yaml")
    b = ProjectBundle(root=root, project=load_project(root / "project.yaml"))
    b.identities = _collect(_yaml_files(root / "identities"), load_identity, "identity")
    b.scenes = _collect(_yaml_files(root / "scenes"), load_scene, "scene")
    b.lines = _collect(_yaml_files(root / "lines"), load_line, "line")
    if (root / "teasers.json").is_file():
        raise ProjectError(
            f"{root}: teasers.json is now versioned and reviewed: move it to teasers/{b.id}-v1.json "
            "and give it id, version and status"
        )
    tdir = root / "teasers"
    b.teasers = _collect(sorted(tdir.glob("*.json")) if tdir.is_dir() else [], load_teasers, "teaser links")
    return b


def file_strings(value: object) -> list[str]:
    """Every string in a file's document, review fields aside: what the spoiler list reads
    (everything, negatives and sources included)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() if k not in REVIEW_KEYS for s in file_strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in file_strings(v)]
    return []


# -- cross-checks ---------------------------------------------------------------------------------


def problems(b: ProjectBundle, frames: set[tuple[str, int]] | None = None) -> list[str]:
    """Everything that is wrong across a project's files, as sentences for its author.

    Each file has already validated on its own; this checks what one file says about another.
    ``frames``: the (id, version) of every frame the server has, when known, the project's own
    (``<project>/<name>``) included. A project may use global frames and its own, never another's.
    """
    out: list[str] = []
    proj = b.project
    if proj.id != b.id:
        out.append(f"project.yaml says id {proj.id!r}, but the directory is {b.id!r}")
    for fr in proj.frames:
        if fr.project not in (None, b.id):
            out.append(f"project.yaml names frame {fr.id}, which is project {fr.project}'s")
        elif frames is not None and fr.key not in frames:
            out.append(f"project.yaml names frame {fr.id} v{fr.version}, which this server doesn't have")
    for kind, table in (("identity", b.identities), ("scene", b.scenes), ("line", b.lines)):
        for obj, path in table.values():
            if obj.project != b.id:
                out.append(f"{path.name}: {kind} {obj.id} belongs to project {obj.project!r}, not {b.id!r}")
    for ident, path in b.identities.values():
        if ident.frame is not None and ident.frame.project not in (None, b.id):
            out.append(f"{path.name}: frame {ident.frame.id} is project {ident.frame.project}'s")
        elif ident.frame is not None and frames is not None and ident.frame.key not in frames:
            out.append(f"{path.name}: frame {ident.frame.id} v{ident.frame.version} doesn't exist")
    for scene, path in b.scenes.values():
        if family(scene.model) != proj.family:
            out.append(f"{path.name}: checkpoint {scene.model} isn't in the project's family {proj.family!r}")
    for links, path in b.teasers.values():
        if links.status != "retired":
            out += [f"{path.name}: {p}" for p in teaser_problems(b, links)]
    return out


def teaser_problems(b: ProjectBundle, links: TeaserLinks) -> list[str]:
    """What is wrong with one version of the teaser links: each must name files that exist and fit."""
    out: list[str] = []
    proj = b.project
    if links.project != b.id:
        out.append(f"the teaser links are for project {links.project!r}, not {b.id!r}")
    allowed = {f.key for f in proj.frames}
    for n, t in links.teasers.items():
        line = b.lines.get(t.line.key)
        ident = b.identities.get(t.identity.key)
        scene = b.scenes.get(t.scene.key)
        if line is None:
            out.append(f"{n}: no line {t.line.id} v{t.line.version}")
        if ident is None:
            out.append(f"{n}: no identity {t.identity.id} v{t.identity.version}")
        if scene is None:
            out.append(f"{n}: no scene {t.scene.id} v{t.scene.version}")
        if t.frame.project not in (None, b.id):
            out.append(f"{n}: frame {t.frame.id} is project {t.frame.project}'s")
        elif allowed and t.frame.key not in allowed:
            out.append(f"{n}: frame {t.frame.id} v{t.frame.version} isn't one of the project's frames")
        if line is not None and ident is not None and line[0].voice != ident[0].voice:
            out.append(f"{n}: the line is in the {line[0].voice!r} voice but the identity speaks as {ident[0].voice!r}")
        if ident is not None and scene is not None:
            if ident[0].presence == "embodied" and scene[0].character_area is None:
                out.append(f"{n}: {ident[0].id} appears in the image, but scene {scene[0].id} has no character_area")
            if ident[0].presence == "viewpoint" and t.frame != ident[0].frame:
                out.append(f"{n}: a viewpoint teaser uses its identity's frame ({ident[0].frame.id})")  # type: ignore[union-attr]
    return out
