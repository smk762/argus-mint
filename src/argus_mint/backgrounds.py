"""Backgrounds: a teaser's first layer, rendered from an approved scene and reviewed one by one.

A scene asks for ``backgrounds`` renders. Each is the scene's fixed prompt (hard rule 1) with a
seed derived from the project, scene and number, so the set is reproducible (hard rule 7); a
safety rejection is retried with the next derived seed and every attempt is recorded (hard
rule 3). The moderator then approves or rejects each image, bound to its sha256, and only an
approved background goes into a teaser (hard rule 9).

The set for one scene version is a directory outside the repository::

    <ARGUS_MINT_BACKGROUNDS_DIR>/<project>/<scene>-v<version>/
      manifest.json              # BackgroundSet: every background, every failure, the last job
      <scene>-v<version>-01.png  # exactly as darkroom returned it (its sha256 is darkroom's record)

Rendering and storing only; the HTTP routes and the job thread live in ``server_teasers``.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from argus_mint._codec import hex0x
from argus_mint.cards import write_atomic
from argus_mint.projects import Project, Scene
from argus_mint.render import Darklight, Rendered, render_requests
from argus_mint.templates import SEED_MAX, RenderRequest

__all__ = [
    "Background",
    "BackgroundError",
    "BackgroundJob",
    "BackgroundReview",
    "BackgroundSet",
    "BackgroundStore",
    "FailedBackground",
    "background_id",
    "background_seed",
    "parse_background_id",
    "render_background",
    "scene_request",
]

MANIFEST = "manifest.json"
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_BACKGROUND_ID = re.compile(r"^(?P<scene>[a-z0-9][a-z0-9-]{0,63})-v(?P<version>[1-9][0-9]{0,5})-(?P<n>[0-9]{2})$")


class BackgroundError(ValueError):
    """A background can't be rendered, found or reviewed as asked."""


def background_id(scene: Scene, n: int) -> str:
    """``<scene>-v<version>-<nn>``: unique within a project, and names the scene version it came from."""
    return f"{scene.id}-v{scene.version}-{n:02d}"


def parse_background_id(bg_id: str) -> tuple[str, int, int]:
    """(scene id, scene version, number) of a background id; :class:`BackgroundError` if it isn't one."""
    m = _BACKGROUND_ID.fullmatch(bg_id)
    if m is None:
        raise BackgroundError(f"not a background id: {bg_id!r}")
    return m["scene"], int(m["version"]), int(m["n"])


def background_seed(project: str, scene: Scene, n: int) -> int:
    """The seed asked for background ``n`` of ``scene``: the same every time (hard rule 7)."""
    digest = hashlib.sha256(f"argus-mint/background/{project}/{scene.id}/{scene.version}/{n}".encode()).digest()
    return int.from_bytes(digest[:4], "big") & SEED_MAX


def scene_request(scene: Scene, seed: int) -> RenderRequest:
    """The render request for one background: the scene's fixed prompt and settings, no LoRAs.

    The request's ``template_*`` name the scene (its id, version and hash).
    """
    return RenderRequest(
        template_id=scene.id,
        template_version=scene.version,
        template_hash=scene.hash(),
        model=scene.model,
        loras=(),
        sampling=scene.sampling,
        width=scene.width,
        height=scene.height,
        prompt=scene.prompt,
        negative=scene.negative,
        variables={},
        seed=seed,
    )


def render_background(client: Darklight, project: Project, scene: Scene, n: int) -> Rendered:
    """Render background ``n`` of an approved scene in an approved project.

    Raises :class:`BackgroundError` for a file that isn't approved (hard rule 2), and
    ``RenderError`` / ``RenderFailed`` from :func:`argus_mint.render.render_requests`.
    """
    for kind, obj in (("project", project), ("scene", scene)):
        if obj.status != "approved":
            raise BackgroundError(f"{kind} {obj.id} v{obj.version} is {obj.status}; only approved scenes render")
    if scene.project != project.id:
        raise BackgroundError(f"scene {scene.id} belongs to project {scene.project!r}, not {project.id!r}")
    if not 1 <= n <= scene.backgrounds:
        raise BackgroundError(f"scene {scene.id} v{scene.version} asks for {scene.backgrounds} backgrounds, not #{n}")
    return render_requests(
        client,
        lambda seed: scene_request(scene, seed),
        background_seed(project.id, scene, n),
        scene.max_attempts,
        label=f"scene {scene.id} v{scene.version} background {n}",
    )


class BackgroundReview(BaseModel):
    """The moderator's decision on one background image, bound to its sha256."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: Literal["approved", "rejected"]
    by: str = Field(min_length=1, max_length=128)
    at: datetime
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    note: str = Field(default="", max_length=2000)


class Background(BaseModel):
    """One rendered background and its review. ``status`` is ``pending`` until the moderator decides."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    n: int = Field(ge=1)
    seed: int  # the seed asked for; the request's is the final one
    request: RenderRequest
    attempts: tuple[dict[str, Any], ...]
    file: str  # relative to the set's directory
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    made_with: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    status: Literal["pending", "approved", "rejected"] = "pending"
    review: BackgroundReview | None = None

    @model_validator(mode="after")
    def _status_is_the_review(self) -> Background:
        if self.file != f"{self.id}.png":
            raise ValueError(f"a background's file is {self.id}.png")
        want = "pending" if self.review is None else self.review.decision
        if self.status != want:
            raise ValueError(f"status {self.status!r} doesn't match the review ({want})")
        if self.review is not None and self.review.image_sha256 != self.sha256:
            raise ValueError("the review is of another image")
        return self


class FailedBackground(BaseModel):
    """A background that made no image. ``final``: the safety check refused every attempt, so
    rendering it again would only repeat them; otherwise an error stopped it and a new job retries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n: int = Field(ge=1)
    seed: int
    error: str
    final: bool
    attempts: tuple[dict[str, Any], ...]


class BackgroundJob(BaseModel):
    """The last rendering job for a set. A job left queued or running by a restart reads as failed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[0-9a-f]{16}$")
    status: Literal["queued", "running", "done", "failed"]
    created_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    numbers: tuple[int, ...]  # which backgrounds it renders


class BackgroundSet(BaseModel):
    """``manifest.json``: the backgrounds of one scene version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal[1] = 1
    project: str
    scene: str
    scene_version: int = Field(ge=1)
    scene_hash: str = Field(pattern=r"^0x[0-9a-f]{64}$")
    wanted: int = Field(ge=1)  # the scene's `backgrounds`
    backgrounds: tuple[Background, ...] = ()
    failed: tuple[FailedBackground, ...] = ()
    job: BackgroundJob | None = None

    def get(self, bg_id: str) -> Background | None:
        return next((b for b in self.backgrounds if b.id == bg_id), None)

    def missing(self) -> list[int]:
        """Numbers still to render: no image yet, and not finally refused by the safety check."""
        done = {b.n for b in self.backgrounds} | {f.n for f in self.failed if f.final}
        return [n for n in range(1, self.wanted + 1) if n not in done]


class BackgroundStore:
    """Background sets under ``root``, one directory per scene version. Ids are checked before any path is built.

    ``lock`` serialises every read-modify-write of a manifest (the job thread and review requests).
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.lock = threading.Lock()

    def dir(self, project: str, scene: str, version: int) -> Path:
        if not _ID.fullmatch(project) or not _ID.fullmatch(scene) or version < 1:
            raise BackgroundError(f"not a scene: {project}/{scene} v{version}")
        return self.root / project / f"{scene}-v{version}"

    def load(self, project: str, scene: str, version: int) -> BackgroundSet | None:
        f = self.dir(project, scene, version) / MANIFEST
        if not f.is_file():
            return None
        try:
            return BackgroundSet.model_validate(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError) as e:
            raise BackgroundError(f"{f}: {e}") from e

    def save(self, s: BackgroundSet) -> None:
        d = self.dir(s.project, s.scene, s.scene_version)
        d.mkdir(parents=True, exist_ok=True)
        write_atomic(d / MANIFEST, s.model_dump_json(indent=2) + "\n")

    def sets(self, project: str) -> list[BackgroundSet]:
        """Every set of a project, by scene id then version."""
        if not _ID.fullmatch(project) or not (self.root / project).is_dir():
            return []
        out = []
        for d in sorted((self.root / project).iterdir()):
            m = re.fullmatch(r"(?P<scene>[a-z0-9][a-z0-9-]{0,63})-v(?P<v>[1-9][0-9]{0,5})", d.name)
            if m and (d / MANIFEST).is_file():
                s = self.load(project, m["scene"], int(m["v"]))
                if s is not None:
                    out.append(s)
        return sorted(out, key=lambda s: (s.scene, s.scene_version))

    def start(self, project: str, scene: Scene) -> BackgroundSet:
        """The set for ``scene``, created when it has none. Refuses a set made from other scene text."""
        s = self.load(project, scene.id, scene.version)
        want = hex0x(scene.hash())
        if s is None:
            s = BackgroundSet(
                project=project, scene=scene.id, scene_version=scene.version, scene_hash=want, wanted=scene.backgrounds
            )
        elif s.scene_hash != want:
            raise BackgroundError(f"the backgrounds of {scene.id} v{scene.version} were rendered from another file")
        return s

    def find(self, project: str, bg_id: str) -> tuple[BackgroundSet, Background]:
        scene, version, _n = parse_background_id(bg_id)
        s = self.load(project, scene, version)
        b = s.get(bg_id) if s is not None else None
        if s is None or b is None:
            raise BackgroundError(f"no background {bg_id} in project {project}")
        return s, b

    def image(self, project: str, bg_id: str) -> bytes:
        """The background's PNG, checked against the sha256 it was recorded (and reviewed) with."""
        s, b = self.find(project, bg_id)
        data = (self.dir(project, s.scene, s.scene_version) / b.file).read_bytes()
        if hashlib.sha256(data).hexdigest() != b.sha256:
            raise BackgroundError(f"background {bg_id} on disk differs from the one rendered")
        return data

    def add(self, project: str, scene: Scene, n: int, rendered: Rendered, made_with: dict[str, str]) -> Background:
        """Store a new render as a pending background (replacing an earlier failure of the same number)."""
        bg_id = background_id(scene, n)
        b = Background(
            id=bg_id,
            n=n,
            seed=background_seed(project, scene, n),
            request=rendered.request,
            attempts=tuple(a.to_json() for a in rendered.attempts),
            file=f"{bg_id}.png",
            sha256=rendered.image_sha256,
            made_with=made_with,
            created_at=datetime.now(UTC),
        )
        with self.lock:
            s = self.start(project, scene)
            if s.get(bg_id) is not None:
                raise BackgroundError(f"background {bg_id} is already rendered")
            d = self.dir(project, scene.id, scene.version)
            d.mkdir(parents=True, exist_ok=True)
            (d / b.file).write_bytes(rendered.image)
            self.save(
                s.model_copy(
                    update={
                        "backgrounds": tuple(sorted((*s.backgrounds, b), key=lambda x: x.n)),
                        "failed": tuple(f for f in s.failed if f.n != n),
                    }
                )
            )
        return b

    def fail(self, project: str, scene: Scene, n: int, error: str, final: bool, attempts: list[dict[str, Any]]) -> None:
        f = FailedBackground(
            n=n, seed=background_seed(project, scene, n), error=error, final=final, attempts=tuple(attempts)
        )
        with self.lock:
            s = self.start(project, scene)
            failed = tuple(sorted((*(x for x in s.failed if x.n != n), f), key=lambda x: x.n))
            self.save(s.model_copy(update={"failed": failed}))

    def set_job(self, project: str, scene: Scene, job: BackgroundJob) -> None:
        with self.lock:
            self.save(self.start(project, scene).model_copy(update={"job": job}))

    def review(self, project: str, bg_id: str, *, approve: bool, by: str, note: str = "") -> Background:
        """Record the moderator's decision on a pending background. Each background is decided once."""
        with self.lock:
            s, b = self.find(project, bg_id)
            if b.review is not None:
                raise BackgroundError(f"background {bg_id} was already {b.review.decision}")
            image_sha = hashlib.sha256(self.image(project, bg_id)).hexdigest()  # what the moderator saw
            decision = BackgroundReview(
                decision="approved" if approve else "rejected",
                by=by,
                at=datetime.now(UTC),
                image_sha256=image_sha,
                note=note,
            )
            decided = b.model_copy(update={"review": decision, "status": decision.decision})
            Background.model_validate(decided.model_dump())  # status and review agree
            self.save(
                s.model_copy(update={"backgrounds": tuple(decided if x.id == bg_id else x for x in s.backgrounds)})
            )
        return decided
