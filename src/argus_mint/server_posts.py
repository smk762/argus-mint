"""HTTP routes for posting approved teasers (argus-herald is the poster).

=================================  ===============================================================
``GET  /posts/ready``              approved teaser drafts a poster may post, newest first (open)
``POST /drafts/{id}/posted``       record a post of a teaser's image (poster or moderator token)
=================================  ===============================================================

The poster pulls: it polls ``/posts/ready``, fetches each image from ``/drafts/{id}/image``,
posts it, and reports back. argus-mint never pushes to it. The poster token
(``ARGUS_MINT_POSTER_TOKEN``) is accepted here and nowhere else; the moderator token works here
too.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import Response

from argus_mint.cards import DraftError, DraftNotFound, DraftStore, Post, sha256_hex
from argus_mint.posting import approved_at, is_current, teaser_drafts
from argus_mint.projects import ProjectError, load_bundle
from argus_mint.wire import DraftPosts, PostRequest, ReadyTeaser, ReadyTeasers

_PROJECT = r"^[a-z0-9][a-z0-9-]{0,63}$"


def add_post_routes(
    app: FastAPI,
    *,
    drafts: DraftStore,
    projects_dir: Path,
    require_poster: Callable[[str | None], Literal["poster", "moderator"]],
    lock: threading.Lock,
) -> None:
    @app.get("/posts/ready")
    def ready(
        project: Annotated[str | None, Query(pattern=_PROJECT)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ReadyTeasers:
        """Approved teaser drafts, newest first (by ``created_at``). Posted ones stay listed, marked ``posted``."""
        links: dict[str, Any] = {}

        def current_links(pid: str) -> Any:
            if pid not in links:
                try:
                    links[pid] = load_bundle(projects_dir / pid).current_teasers()
                except (ProjectError, OSError):
                    links[pid] = None
            return links[pid]

        items = []
        for t in teaser_drafts(drafts, project):
            if not t.approved:
                continue
            d, rec = t.draft, t.teaser
            at = approved_at(d)
            assert at is not None
            items.append(
                ReadyTeaser(
                    id=t.id,
                    project=rec.project.id,
                    title=d.name.removesuffix(f" {rec.number}"),
                    number=rec.number,
                    status=t.status,  # type: ignore[arg-type]
                    current=is_current(rec, current_links(rec.project.id)),
                    created_at=d.created_at,
                    approved_at=at,
                    image_url=f"/drafts/{t.id}/image",
                    image_sha256=d.image_sha256,
                    alt=rec.alt,
                    quote=d.texts.get("quote", ""),
                    attribution=d.texts.get("attribution"),
                    label=d.texts.get("label"),
                    posted=bool(t.posts),
                    posts=list(t.posts),
                )
            )
        return ReadyTeasers(items=items[offset : offset + limit], total=len(items))

    @app.post("/drafts/{draft_id}/posted", status_code=201)
    def posted(
        draft_id: str,
        body: PostRequest,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> DraftPosts:
        """Record a post of an approved teaser's image. The same post sent again is recorded once (200)."""
        by = require_poster(authorization)
        with lock:
            try:
                d, _art, image = drafts.load(draft_id)
                if d.kind != "teaser":
                    raise HTTPException(409, "only a teaser draft is posted")
                status = "published" if drafts.published(draft_id) else d.status
                if status not in ("approved", "published"):
                    raise HTTPException(409, f"only an approved teaser is posted (this one is {status})")
                if sha256_hex(image) != d.image_sha256:
                    raise HTTPException(409, "the image on disk differs from the one reviewed")
                if body.sha256 != d.image_sha256:
                    raise HTTPException(409, "sha256 isn't this draft's image: post the reviewed PNG, byte for byte")
                posts = drafts.posts(draft_id)
                same = [
                    p for p in posts if (p.where, p.url, p.at, p.sha256) == (body.where, body.url, body.at, body.sha256)
                ]
                if same:
                    response.status_code = 200
                else:
                    post = Post(
                        where=body.where, url=body.url, at=body.at, sha256=body.sha256,
                        recorded_at=datetime.now(UTC), by=by,
                    )  # fmt: skip
                    posts = drafts.add_post(draft_id, post)
            except DraftNotFound as e:
                raise HTTPException(404, str(e)) from e
            except DraftError as e:
                raise HTTPException(400 if "not a draft id" in str(e) else 500, str(e)) from e
        return DraftPosts(id=draft_id, image_sha256=d.image_sha256, posts=list(posts))
