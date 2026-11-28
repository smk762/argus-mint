"""HTTP routes for a teaser's layers: rendering and reviewing backgrounds, then composing teaser drafts.

=================================================================  ===================================================
``POST /projects/{id}/scenes/{scene}/{version}/backgrounds``      render the scene's missing backgrounds (a job)
``GET  /projects/{id}/backgrounds``                               every background set of the project
``GET  /projects/{id}/scenes/{scene}/{version}/backgrounds``      one scene version's set
``GET  /projects/{id}/backgrounds/{bg}/image``                    a background's PNG
``POST /projects/{id}/backgrounds/{bg}/review``                   the moderator approves or rejects one
``POST /projects/{id}/teasers/{n}/draft``                         a teaser draft from ``T<n>`` (then reviewed as usual)
``POST /drafts/{id}/identity-check``                              run a teaser's identity check again
``GET  /projects/{id}/tracker``                                   each teaser's stages, as mint works them out
=================================================================  ===================================================

Every write needs the moderator token. Background jobs run one at a time on a background
thread, like samples, and ask darkroom for the GPU at ``batch`` priority; a teaser's inpainting
is a person waiting, so it asks at darkroom's default.

A new teaser draft gets its identity check (``identity_check.py``: argus-proof scores the figure
against the identity's references) and auto-moderation's checks before it is saved. When proof
was down the draft is held, and the moderator runs the check again with
``POST /drafts/{id}/identity-check``: a sub-resource of the draft like ``/review`` and ``/edit``,
named for the record it rewrites (``auto_check.identity``) and the capability
(``identity_check``). It reruns the same check, so it isn't called a retry; it changes no image
and no review except the approval auto-moderation records when it is on and every check passes.
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
from argus_cortex.hashing import sha256_hex
from eth_utils import to_checksum_address
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response

import argus_mint
from argus_mint._codec import hex0x
from argus_mint.backgrounds import (
    Background,
    BackgroundError,
    BackgroundJob,
    BackgroundSet,
    BackgroundStore,
    background_loras,
    render_background,
)
from argus_mint.cards import CardDraft, DraftError, DraftNotFound, DraftStore, IdentityCheck
from argus_mint.darkroom import DarkroomError
from argus_mint.frames import FrameError, LoadedFrame
from argus_mint.identity_check import Scorer, check_identity
from argus_mint.posting import is_current
from argus_mint.projects import (
    Identity,
    ProjectBundle,
    ProjectError,
    Scene,
    file_strings,
    load_bundle,
    load_project,
)
from argus_mint.render import RenderError, RenderFailed
from argus_mint.server_projects import is_id
from argus_mint.spoilers import SpoilerList
from argus_mint.teasers import TeaserError, TeaserNotFound, make_teaser
from argus_mint.wire import (
    BackgroundDecision,
    BackgroundSetList,
    CreateTeaserDraft,
    DraftDetail,
    ProjectTracker,
    TeaserDraftState,
    TeaserProgress,
    TeaserStages,
)

log = structlog.get_logger(__name__)

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
    moderate: Callable[[CardDraft, bytes, IdentityCheck | None], CardDraft],
    detail: Callable[[str], DraftDetail],
    proof: Scorer | None = None,
    status_of: Callable[[str, CardDraft], str],
) -> BackgroundRunner:
    store = BackgroundStore(backgrounds_dir)
    runner = BackgroundRunner(store, projects_dir)
    starting = threading.Lock()  # one start at a time, so two requests can't both find the set idle

    def bundle(pid: str) -> ProjectBundle:
        if not is_id(pid) or not (projects_dir / pid / "project.yaml").is_file():
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
        loras = background_loras(b.project)
        check = getattr(darkroom, "check_render", None)
        if check is not None:
            try:
                refused = check(sc.model, len(loras))
            except DarkroomError as e:
                raise HTTPException(502, str(e)) from e
            if refused:
                raise HTTPException(409, "darkroom can't render this scene's backgrounds: " + "; ".join(refused))
        # Background work: darkroom leases the GPU at batch priority, behind a person.
        renderer = darkroom.batch() if hasattr(darkroom, "batch") else darkroom
        with starting:
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
        links = b.current_teasers()
        if links is None:
            raise HTTPException(409, f"project {pid} has no approved teaser links")
        link = links.teasers.get(number)
        if link is None:
            raise HTTPException(404, f"project {pid} has no teaser {number} in its teaser links v{links.version}")
        table = frames()
        lf = table.get(link.frame.key)
        if lf is None:
            raise HTTPException(409, f"frame {link.frame.id} v{link.frame.version} isn't served here")
        if link.background is None:
            raise HTTPException(409, f"{number} has no background chosen yet (its `background` in the teaser links)")
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
        # Scored before the lock: proof may wait for the GPU. A teaser is never saved unchecked.
        checked = check_identity(
            art=made.art, identity=b.identities[link.identity.key][0], scene=b.scenes[link.scene.key][0], proof=proof
        )
        with lock:
            draft_id = drafts.new_id()
            drafts.save(draft_id, moderate(made.draft, made.image, checked), made.art, made.image)
        return detail(draft_id)

    def made_from(d: CardDraft) -> tuple[Identity, Scene]:
        """The identity and scene files a teaser draft names, as they were when it was made."""
        t = d.teaser
        assert t is not None
        b = bundle(t.project.id)
        found = []
        for kind, ref, table in (("identity", t.identity, b.identities), ("scene", t.scene, b.scenes)):
            entry = table.get((ref.id, ref.version))
            if entry is None or hex0x(entry[0].hash()) != ref.hash:
                raise HTTPException(409, f"{kind} {ref.id} v{ref.version} isn't the file this teaser was made from")
            found.append(entry[0])
        return found[0], found[1]

    @app.post("/drafts/{draft_id}/identity-check")
    def post_identity_check(draft_id: str, authorization: Annotated[str | None, Header()] = None) -> DraftDetail:
        """Run a pending teaser's identity check again (proof was down, say), and auto-moderation's checks with it."""
        require_token(authorization)

        def pending() -> tuple[CardDraft, bytes, bytes]:
            try:
                d, art, image = drafts.load(draft_id)
            except DraftNotFound as e:
                raise HTTPException(404, str(e)) from e
            except DraftError as e:
                raise HTTPException(409, str(e)) from e
            if d.kind != "teaser":
                raise HTTPException(409, "only a teaser has an identity check")
            if drafts.published(draft_id):
                raise HTTPException(409, "this draft is already published")
            if d.status != "pending":
                raise HTTPException(409, f"this draft is already {d.status}")
            if sha256_hex(art) != d.art_sha256 or sha256_hex(image) != d.image_sha256:
                raise HTTPException(409, "the images on disk differ from the ones made for this draft")
            return d, art, image

        d, art, _image = pending()
        identity, scene = made_from(d)
        checked = check_identity(art=art, identity=identity, scene=scene, proof=proof)
        with lock:
            now, _art, image = pending()
            if (
                now.art_sha256 != d.art_sha256 or now.image_sha256 != d.image_sha256
            ):  # pragma: no cover - teasers aren't redrawn
                raise HTTPException(409, "the draft changed while it was being checked")
            drafts.save(draft_id, moderate(now, image, checked))
        return detail(draft_id)

    @app.get("/projects/{pid}/tracker")
    def tracker(pid: str) -> ProjectTracker:
        """Per teaser of the newest approved links: which stages it has reached, and its drafts.

        ``quote_final``: its line is approved. ``concept_chosen``: its identity and scene are.
        ``image_made``: a draft made from these same files is approved (or published). ``posted``:
        a draft of the teaser has a post recorded. ``minted``: a draft of it is published (pinned,
        with metadata and a signed voucher). Those two count any draft of the number, current or
        not: a post or a voucher that happened stays a fact.
        """
        b = bundle(pid)
        links = b.current_teasers()
        mine: list[tuple[str, CardDraft]] = []
        for draft_id in drafts.ids():
            try:
                d, _a, _i = drafts.load(draft_id)
            except DraftError:
                continue
            if d.kind == "teaser" and d.teaser is not None and d.teaser.project.id == pid:
                mine.append((draft_id, d))

        def approved(table: dict[tuple[str, int], tuple[Any, Path]], key: tuple[str, int]) -> bool:
            found = table.get(key)
            return found is not None and found[0].status == "approved"

        items: list[TeaserProgress] = []
        for number, link in sorted((links.teasers if links else {}).items(), key=lambda kv: int(kv[0][1:])):
            states = []
            posted = False
            for draft_id, d in mine:
                t = d.teaser
                assert t is not None
                if t.number != number:
                    continue
                current = is_current(t, links)
                if drafts.posts(draft_id):
                    posted = True
                states.append(
                    TeaserDraftState(
                        id=draft_id,
                        status=status_of(draft_id, d),
                        awaiting=d.awaiting(),  # type: ignore[arg-type]
                        created_at=d.created_at,
                        links_version=t.links.version,
                        background=t.background.id,
                        current=current,
                    )  # fmt: skip
                )
            items.append(
                TeaserProgress(
                    number=number,
                    stages=TeaserStages(
                        quote_final=approved(b.lines, link.line.key),
                        concept_chosen=approved(b.identities, link.identity.key)
                        and approved(b.scenes, link.scene.key)
                        and (link.pose is None or approved(b.poses, link.pose.key)),
                        image_made=any(s.current and s.status in ("approved", "published") for s in states),
                        posted=posted,
                        minted=any(s.status == "published" for s in states),
                    ),
                    background=link.background,
                    drafts=states,
                )
            )
        return ProjectTracker(project=pid, links_version=links.version if links else None, teasers=items)

    return runner
