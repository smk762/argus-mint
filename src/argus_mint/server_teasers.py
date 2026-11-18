"""HTTP routes for a teaser's layers: rendering and reviewing backgrounds, then composing teaser drafts.

=================================================================  ===================================================
``POST /projects/{id}/scenes/{scene}/{version}/backgrounds``      render the scene's missing backgrounds (a job)
``GET  /projects/{id}/backgrounds``                               every background set of the project
``GET  /projects/{id}/scenes/{scene}/{version}/backgrounds``      one scene version's set
``GET  /projects/{id}/backgrounds/{bg}/image``                    a background's PNG
``POST /projects/{id}/backgrounds/{bg}/review``                   the moderator approves or rejects one
``POST /projects/{id}/teasers/{n}/draft``                         a teaser draft from ``T<n>`` (then reviewed as usual)
=================================================================  ===================================================

Every write needs the moderator token. Background jobs run one at a time on a background
thread, like samples, and ask darkroom for the GPU at ``batch`` priority; a teaser's inpainting
is a person waiting, so it asks at darkroom's default.
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
from eth_utils import to_checksum_address
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response

import argus_mint
from argus_mint.backgrounds import (
    Background,
    BackgroundError,
    BackgroundJob,
    BackgroundSet,
    BackgroundStore,
    render_background,
)
from argus_mint.cards import CardDraft, DraftStore
from argus_mint.darkroom import DarkroomError
from argus_mint.frames import FrameError, LoadedFrame
from argus_mint.projects import ProjectBundle, ProjectError, Scene, file_strings, load_bundle, load_project
from argus_mint.render import RenderError, RenderFailed
from argus_mint.spoilers import SpoilerList
from argus_mint.teasers import TeaserError, TeaserNotFound, make_teaser
from argus_mint.wire import BackgroundDecision, BackgroundSetList, CreateTeaserDraft, DraftDetail

log = structlog.get_logger(__name__)

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_TEASER = re.compile(r"^T[1-9][0-9]{0,4}$")


@dataclass
class _Job:
    id: str
    project: str
    scene: Scene
    numbers: tuple[int, ...]
    renderer: Any


@dataclass
class BackgroundRunner:
    """Background jobs, one at a time on one thread. ``live`` holds the jobs this process still owns:
    a set whose job is queued or running but not live was cut off by a restart."""

    store: BackgroundStore
    projects_dir: Path
    jobs: queue.Queue[_Job] = field(default_factory=queue.Queue)
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    live: set[str] = field(default_factory=set)

    def shown(self, s: BackgroundSet) -> BackgroundSet:
        """``s`` as a client should see it: an interrupted job reads as failed."""
        j = s.job
        if j is not None and j.status in ("queued", "running") and j.id not in self.live:
            return s.model_copy(
                update={"job": j.model_copy(update={"status": "failed", "error": "interrupted: the server stopped"})}
            )
        return s

    def busy(self, s: BackgroundSet) -> bool:
        return s.job is not None and s.job.status in ("queued", "running") and s.job.id in self.live

    def submit(self, project: str, scene: Scene, numbers: list[int], renderer: Any) -> BackgroundJob:
        job = BackgroundJob(
            id=secrets.token_hex(8), status="queued", created_at=datetime.now(UTC), numbers=tuple(numbers)
        )
        with self.lock:
            self.live.add(job.id)
            self.store.set_job(project, scene, job)
            self.jobs.put(_Job(job.id, project, scene, tuple(numbers), renderer))
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._work, name="argus-mint-backgrounds", daemon=True)
                self.thread.start()
        return job

    def _work(self) -> None:
        while True:
            try:
                job = self.jobs.get(timeout=5)
            except queue.Empty:
                with self.lock:  # retire under submit()'s lock, so a job queued meanwhile isn't stranded
                    if self.jobs.empty():
                        self.thread = None
                        return
                continue
            try:
                self._run(job)
            except Exception as e:  # e.g. the manifest can't be written: keep working through the queue
                log.warning("background_job_crashed", job=job.id, error=str(e))
            finally:
                self.live.discard(job.id)

    def _set(self, job: _Job, **update: Any) -> None:
        s = self.store.load(job.project, job.scene.id, job.scene.version)
        current = s.job if s is not None and s.job is not None and s.job.id == job.id else None
        if current is not None:
            self.store.set_job(job.project, job.scene, current.model_copy(update=update))

    def _run(self, job: _Job) -> None:
        self._set(job, status="running")
        try:
            for n in job.numbers:
                project = load_project(
                    self.projects_dir / job.project / "project.yaml"
                )  # re-read: it may have been retired
                try:
                    rendered = render_background(job.renderer, project, job.scene, n)
                except RenderFailed as e:
                    self.store.fail(job.project, job.scene, n, str(e), True, [a.to_json() for a in e.attempts])
                    continue
                except RenderError as e:
                    self.store.fail(job.project, job.scene, n, str(e), False, [a.to_json() for a in e.attempts])
                    continue
                made_with = {"argus-mint": argus_mint.__version__}
                if rendered.renderer_version:
                    made_with["darkroom"] = rendered.renderer_version
                self.store.add(job.project, job.scene, n, rendered, made_with)
        except Exception as e:  # a backend failure ends the job; it is reported, not raised
            log.warning("background_job_failed", job=job.id, error=str(e))
            self._set(job, status="failed", error=str(e), finished_at=datetime.now(UTC))
            return
        self._set(job, status="done", finished_at=datetime.now(UTC))


def add_teaser_routes(
    app: FastAPI,
    *,
    projects_dir: Path,
    backgrounds_dir: Path,
    spoilers: SpoilerList,
    darkroom: Any,
    require_token: Callable[[str | None], None],
    lock: threading.Lock,
    frames: Callable[[], dict[tuple[str, int], LoadedFrame]],
    drafts: DraftStore,
    moderate: Callable[[CardDraft, bytes], CardDraft],
    detail: Callable[[str], DraftDetail],
) -> BackgroundRunner:
    store = BackgroundStore(backgrounds_dir)
    runner = BackgroundRunner(store, projects_dir)

    def bundle(pid: str) -> ProjectBundle:
        if not _ID.fullmatch(pid) or not (projects_dir / pid / "project.yaml").is_file():
            raise HTTPException(404, f"no project {pid!r}")
        try:
            return load_bundle(projects_dir / pid)
        except ProjectError as e:
            raise HTTPException(422, str(e)) from e

    def scene_of(b: ProjectBundle, scene: str, version: int) -> Scene:
        found = b.scenes.get((scene, version))
        if found is None:
            raise HTTPException(404, f"no scene {scene} v{version} in project {b.id}")
        return found[0]

    def background_set(pid: str, scene: str, version: int) -> BackgroundSet:
        try:
            s = store.load(pid, scene, version)
        except BackgroundError as e:
            raise HTTPException(404 if "not a scene" in str(e) else 500, str(e)) from e
        if s is None:
            raise HTTPException(404, f"scene {scene} v{version} of project {pid} has no backgrounds yet")
        return runner.shown(s)

    @app.post("/projects/{pid}/scenes/{scene}/{version}/backgrounds", status_code=202)
    def start_backgrounds(
        pid: str, scene: str, version: int, authorization: Annotated[str | None, Header()] = None
    ) -> BackgroundSet:
        """Render the scene's backgrounds that have no image yet (and weren't finally refused by the safety check)."""
        require_token(authorization)
        if darkroom is None:
            raise HTTPException(503, "no darkroom is configured (ARGUS_MINT_DARKROOM_URL)")
        b = bundle(pid)
        sc = scene_of(b, scene, version)
        for kind, obj in (("project", b.project), ("scene", sc)):
            if obj.status != "approved":
                raise HTTPException(409, f"{kind} {obj.id} v{obj.version} is {obj.status}; only approved scenes render")
        hits = spoilers.hits(*file_strings(sc.model_dump(mode="json")))
        if hits:
            raise HTTPException(409, "held by spoiler rule " + ", ".join(map(str, hits)))
        with store.lock:
            try:
                s = store.start(pid, sc)
            except BackgroundError as e:
                raise HTTPException(409, str(e)) from e
        if runner.busy(s):
            raise HTTPException(409, f"a job is already rendering scene {scene} v{version}")
        numbers = s.missing()
        if not numbers:
            raise HTTPException(409, f"every background of scene {scene} v{version} is rendered")
        # Background work: darkroom leases the GPU at batch priority, behind a person.
        renderer = darkroom.batch() if hasattr(darkroom, "batch") else darkroom
        runner.submit(pid, sc, numbers, renderer)
        return background_set(pid, scene, version)

    @app.get("/projects/{pid}/backgrounds")
    def list_backgrounds(pid: str) -> BackgroundSetList:
        bundle(pid)
        try:
            return BackgroundSetList(sets=[runner.shown(s) for s in store.sets(pid)])
        except BackgroundError as e:
            raise HTTPException(500, str(e)) from e

    @app.get("/projects/{pid}/scenes/{scene}/{version}/backgrounds")
    def get_backgrounds(pid: str, scene: str, version: int) -> BackgroundSet:
        bundle(pid)
        return background_set(pid, scene, version)

    @app.get("/projects/{pid}/backgrounds/{bg_id}/image")
    def background_image(pid: str, bg_id: str) -> Response:
        bundle(pid)
        try:
            return Response(store.image(pid, bg_id), media_type="image/png")
        except BackgroundError as e:
            raise HTTPException(404 if "differs" not in str(e) else 500, str(e)) from e

    @app.post("/projects/{pid}/backgrounds/{bg_id}/review")
    def review_background(
        pid: str, bg_id: str, body: BackgroundDecision, authorization: Annotated[str | None, Header()] = None
    ) -> Background:
        require_token(authorization)
        bundle(pid)
        try:
            return store.review(pid, bg_id, approve=body.decision == "approve", by=body.by, note=body.note)
        except BackgroundError as e:
            missing = str(e).startswith(("no background", "not a background"))
            raise HTTPException(404 if missing else 409, str(e)) from e

    @app.post("/projects/{pid}/teasers/{number}/draft", status_code=201)
    def post_teaser_draft(
        pid: str, number: str, body: CreateTeaserDraft, authorization: Annotated[str | None, Header()] = None
    ) -> DraftDetail:
        """A teaser draft from ``T<n>``: refused unless every part of it is approved."""
        require_token(authorization)
        if not _TEASER.fullmatch(number):
            raise HTTPException(404, f"teasers are numbered T1, T2…, not {number!r}")
        try:
            to = to_checksum_address(body.to)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        b = bundle(pid)
        link = b.teasers.teasers.get(number) if b.teasers else None
        if link is None:
            raise HTTPException(404, f"project {pid} has no teaser {number}")
        table = frames()
        lf = table.get(link.frame.key)
        if lf is None:
            raise HTTPException(409, f"frame {link.frame.id} v{link.frame.version} isn't served here")
        if link.background is None:
            raise HTTPException(409, f"{number} has no background chosen yet (teasers.json `background`)")
        try:
            _s, bg = store.find(pid, link.background)
            bg_image = store.image(pid, link.background)
        except BackgroundError as e:
            raise HTTPException(409, f"{number}'s background: {e}") from e
        try:
            made = make_teaser(
                bundle=b, number=number, frame=lf, background=bg, background_image=bg_image, to=to,
                renderer=darkroom, spoilers=spoilers, frames=set(table),
            )  # fmt: skip
        except TeaserNotFound as e:
            raise HTTPException(404, str(e)) from e
        except TeaserError as e:
            raise HTTPException(409, str(e)) from e
        except RenderError as e:
            raise HTTPException(422, {"error": str(e), "attempts": [a.to_json() for a in e.attempts]}) from e
        except DarkroomError as e:
            raise HTTPException(502, str(e)) from e
        except FrameError as e:
            raise HTTPException(422, str(e)) from e
        with lock:
            draft_id = drafts.new_id()
            drafts.save(draft_id, moderate(made.draft, made.image), made.art, made.image)
        return detail(draft_id)

    return runner
