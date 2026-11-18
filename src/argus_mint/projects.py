"""Teaser projects: the reviewed files behind image + quote teasers.

A project lives in one directory::

    <projects>/<project-id>/
      project.yaml          # title, attribution, frames, SingGuard policy, checkpoint family
      identities/*.yaml     # who appears (or whose view it is), and how they keep their look
      scenes/*.yaml         # the backgrounds: prompt, sampling, how many to render, where a figure goes
      lines/*.yaml          # the quotes, verbatim, with the published wording and their source
      teasers.json          # which line goes with which identity, scene and frame, and its alt text

Every YAML file is versioned and reviewed like a template (``draft -> approved -> retired``,
``catalogue.record_decision``), so only approved files make teasers (hard rule 2). A scene's
prompt is fixed text written by the author and reviewed before anything renders from it; a
teaser never takes a prompt (hard rule 1). ``teasers.json`` only links reviewed files: the
image it leads to is reviewed as a draft, by the moderator and the owner.

Pure: reading the files and checking them, no network. :func:`problems` lists everything that
is wrong with a project at once, for the author, rather than stopping at the first.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from argus_mint._codec import canonical, keccak256
from argus_mint.templates import LoraRef, Sampling, family

__all__ = [
    "Identity",
    "Line",
    "Project",
    "ProjectBundle",
    "ProjectError",
    "Reference",
    "Ref",
    "Scene",
    "TeaserLink",
    "TeaserLinks",
    "file_strings",
    "load_bundle",
    "load_identity",
    "load_line",
    "load_project",
    "load_scene",
    "load_teasers",
    "problems",
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


class _Reviewed(BaseModel):
    """Fields every reviewed project file has, with the approval rule templates use."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=_ID_PATTERN)
    version: int = Field(ge=1)
    status: Status
    approved_by: str | None = None
    approved_at: date | None = None

    @model_validator(mode="after")
    def _approval_recorded(self) -> _Reviewed:
        if self.status != "draft" and (not self.approved_by or self.approved_at is None):
            raise ValueError(f"status {self.status!r} requires approved_by and approved_at (approval is a human step)")
        return self

    @property
    def key(self) -> tuple[str, int]:
        return (self.id, self.version)

    def hash(self) -> bytes:
        """keccak256 of the file's canonical JSON, excluding ``status`` (as for templates)."""
        return keccak256(canonical(self.model_dump(mode="json", exclude={"status"})))


class Project(_Reviewed):
    """The project's settings: what every teaser in it shares."""

    title: str = Field(min_length=1, max_length=120)  # the working title; what is posted becomes the public name
    attribution: str | None = Field(default=None, max_length=120)  # a fixed line under each quote; None: unsigned
    generated_label: bool = True  # draw "generated image" (the frame's `label` slot) on each teaser
    frames: tuple[Ref, ...] = ()  # frames teasers may use (one per aspect, or one per voice)
    policy: str = Field(default="safe", min_length=1, max_length=64)  # SingGuard policy for this project's text
    family: str = Field(default="sdxl", min_length=1, max_length=32)  # checkpoint family (inpainting is SDXL-only)
    style_lora: LoraRef | None = None  # the project's look, trained once enough is approved


class Reference(BaseModel):
    """An image an identity is trained or checked against, with where it came from.

    A reference showing a real person needs a consent record (argus-cortex ``ConsentRecord``,
    checked by the server); a generated or licensed image does not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = Field(min_length=1)  # relative to the identity file
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: Literal["generated", "licensed", "personal"]
    consent: str | None = Field(default=None, min_length=1, max_length=128)  # consent record id
    held_out: bool = False  # kept out of training, so proof can score against it

    @model_validator(mode="after")
    def _consent_for_people(self) -> Reference:
        if self.provenance == "personal" and not self.consent:
            raise ValueError(f"reference {self.file}: a personal photo needs a consent record (`consent`)")
        return self


class Identity(_Reviewed):
    """Someone a teaser speaks for.

    ``embodied``: they appear in the image, painted in from ``prompt`` and kept consistent by
    ``lora`` once one has passed proof. ``viewpoint``: the image is what they see; ``frame``
    (a reviewed overlay, such as a security feed) is what makes it theirs, and nothing is
    painted in.
    """

    project: str = Field(pattern=_ID_PATTERN)
    voice: Voice = Field(min_length=1, max_length=32)
    presence: Literal["embodied", "viewpoint"]
    prompt: str = Field(default="", max_length=2000)  # appearance, for an embodied identity
    negative: str = Field(default="", max_length=2000)
    frame: Ref | None = None  # the overlay of a viewpoint identity
    lora: LoraRef | None = None  # pinned from proof's manifest once a training run passes
    proof_run: str | None = Field(default=None, min_length=1, max_length=128)  # the run that passed it
    references: tuple[Reference, ...] = ()

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
        if (self.lora is None) != (self.proof_run is None):
            raise ValueError("a LoRA and the proof run that passed it go together")
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

    @property
    def shown(self) -> str:
        return self.published or self.text


class TeaserLink(BaseModel):
    """One teaser: a reviewed line, identity, scene and frame, and the image's alt text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line: Ref
    identity: Ref
    scene: Ref
    frame: Ref
    alt: str = Field(min_length=1, max_length=1000)  # posts refuse an image without it
    background: str | None = Field(default=None, min_length=1, max_length=64)  # the chosen render, once there is one


class TeaserLinks(BaseModel):
    """``teasers.json``: the project's teasers by number."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project: str = Field(pattern=_ID_PATTERN)
    teasers: dict[str, TeaserLink] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _numbers(self) -> TeaserLinks:
        bad = [k for k in self.teasers if not re.fullmatch(_TEASER_ID, k)]
        if bad:
            raise ValueError(f"teaser numbers are T1, T2…: {bad}")
        return self


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
    teasers: TeaserLinks | None = None

    @property
    def id(self) -> str:
        return self.root.name


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
        b.teasers = load_teasers(root / "teasers.json")
    return b


REVIEW_KEYS = ("status", "approved_by", "approved_at")


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
    ``frames``: the (id, version) of every frame the server has, when known.
    """
    out: list[str] = []
    proj = b.project
    if proj.id != b.id:
        out.append(f"project.yaml says id {proj.id!r}, but the directory is {b.id!r}")
    for fr in proj.frames:
        if frames is not None and fr.key not in frames:
            out.append(f"project.yaml names frame {fr.id} v{fr.version}, which this server doesn't have")
    for kind, table in (("identity", b.identities), ("scene", b.scenes), ("line", b.lines)):
        for obj, path in table.values():
            if obj.project != b.id:
                out.append(f"{path.name}: {kind} {obj.id} belongs to project {obj.project!r}, not {b.id!r}")
    for ident, path in b.identities.values():
        if ident.frame is not None and frames is not None and ident.frame.key not in frames:
            out.append(f"{path.name}: frame {ident.frame.id} v{ident.frame.version} doesn't exist")
    for scene, path in b.scenes.values():
        if family(scene.model) != proj.family:
            out.append(f"{path.name}: checkpoint {scene.model} isn't in the project's family {proj.family!r}")
    if b.teasers is not None:
        if b.teasers.project != b.id:
            out.append(f"teasers.json is for project {b.teasers.project!r}, not {b.id!r}")
        allowed = {f.key for f in proj.frames}
        for n, t in b.teasers.teasers.items():
            line = b.lines.get(t.line.key)
            ident = b.identities.get(t.identity.key)
            scene = b.scenes.get(t.scene.key)
            if line is None:
                out.append(f"{n}: no line {t.line.id} v{t.line.version}")
            if ident is None:
                out.append(f"{n}: no identity {t.identity.id} v{t.identity.version}")
            if scene is None:
                out.append(f"{n}: no scene {t.scene.id} v{t.scene.version}")
            if allowed and t.frame.key not in allowed:
                out.append(f"{n}: frame {t.frame.id} v{t.frame.version} isn't one of the project's frames")
            if line is not None and ident is not None and line[0].voice != ident[0].voice:
                out.append(
                    f"{n}: the line is in the {line[0].voice!r} voice but the identity speaks as {ident[0].voice!r}"
                )
            if ident is not None and scene is not None:
                if ident[0].presence == "embodied" and scene[0].character_area is None:
                    out.append(
                        f"{n}: {ident[0].id} appears in the image, but scene {scene[0].id} has no character_area"
                    )
                if ident[0].presence == "viewpoint" and t.frame != ident[0].frame:
                    out.append(f"{n}: a viewpoint teaser uses its identity's frame ({ident[0].frame.id})")  # type: ignore[union-attr]
    return out
