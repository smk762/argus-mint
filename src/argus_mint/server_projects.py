"""HTTP routes for teaser projects: list, read, author (draft files only), check and review.

Authoring is the one place prompt text reaches argus-mint from a client, so it is held tight:

- only the moderator writes, and only ``draft`` files (an approved or retired file never
  changes; the author saves a new version);
- every save is checked: the file's schema, the project's cross-file problems, the private
  spoiler list (reported by rule number only) and SingGuard, through darkroom's text pre-flight
  under the project's own policy;
- approval is refused while a spoiler rule matches or SingGuard hasn't passed the file's text,
  and with no darkroom configured there is no SingGuard, so nothing is approved;
- nothing renders from a file until it is approved (hard rule 2); renders are another module's.

``teasers.json`` only links reviewed files, so it is saved without review; the teaser images it
leads to are reviewed as drafts.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import structlog
import yaml
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ValidationError

from argus_mint._codec import hex0x
from argus_mint.catalogue import CatalogueError, record_decision
from argus_mint.projects import (
    Identity,
    Line,
    Project,
    ProjectBundle,
    ProjectError,
    Scene,
    load_bundle,
    load_identity,
    load_line,
    load_project,
    load_scene,
    problems,
)
from argus_mint.spoilers import SpoilerList
from argus_mint.wire import (
    CatalogueDecision,
    ProjectChecks,
    ProjectDetail,
    ProjectFile,
    ProjectList,
    ProjectSummary,
    SavedProjectFile,
    SaveProjectFile,
    SaveTeasers,
    TextCheck,
)

log = structlog.get_logger()

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_REVIEW_KEYS = ("status", "approved_by", "approved_at")


class _Kind:
    def __init__(
        self, subdir: str | None, loader: Callable[[Path], Any], model: type[BaseModel], guarded: tuple[str, ...]
    ):
        self.subdir = subdir  # None: project.yaml at the project's root
        self.loader = loader
        self.model = model
        self.guarded = guarded  # fields SingGuard reads (negative prompts are left out: they name what to avoid)


KINDS: dict[str, _Kind] = {
    "project": _Kind(None, load_project, Project, ("title", "attribution")),
    "identity": _Kind("identities", load_identity, Identity, ("prompt",)),
    "scene": _Kind("scenes", load_scene, Scene, ("prompt",)),
    "line": _Kind("lines", load_line, Line, ("text", "published")),
}


def _strings(value: Any) -> list[str]:
    """Every string in a document, for the spoiler list (which reads everything, negatives included)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() if k not in _REVIEW_KEYS for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def _doc(obj: BaseModel) -> dict[str, Any]:
    return obj.model_dump(mode="json")


def add_project_routes(
    app: FastAPI,
    *,
    projects_dir: Path,
    spoilers: SpoilerList,
    darkroom: Any,
    require_token: Callable[[str | None], None],
    lock: threading.Lock,
    frame_keys: Callable[[], set[tuple[str, int]]],
) -> None:
    guarded: dict[tuple[str, str], tuple[bool, str]] = {}  # (policy, text) -> verdict, for this process's life
    can_guard = hasattr(darkroom, "check_text")

    def project_root(pid: str) -> Path:
        if not _ID.fullmatch(pid):
            raise HTTPException(404, f"no project {pid!r}")
        return projects_dir / pid

    def bundle(pid: str) -> ProjectBundle:
        root = project_root(pid)
        if not (root / "project.yaml").is_file():
            raise HTTPException(404, f"no project {pid}")
        try:
            return load_bundle(root)
        except ProjectError as e:
            raise HTTPException(422, str(e)) from e

    def table(b: ProjectBundle, kind: str) -> dict[tuple[str, int], tuple[Any, Path]]:
        if kind == "project":
            return {b.project.key: (b.project, b.root / "project.yaml")}
        return {"identity": b.identities, "scene": b.scenes, "line": b.lines}[kind]

    def project_file(b: ProjectBundle, kind: str, obj: Any, path: Path) -> ProjectFile:
        return ProjectFile(
            kind=kind,  # type: ignore[arg-type]
            id=obj.id,
            version=obj.version,
            status=obj.status,
            hash=hex0x(obj.hash()),
            file=path.relative_to(b.root).as_posix(),
            doc=_doc(obj),
        )

    def guard_texts(kind: str, obj: Any) -> list[tuple[str, str]]:
        label = f"{kind} {obj.id} v{obj.version}"
        return [
            (f"{label}: {f}", t) for f in KINDS[kind].guarded if isinstance(t := getattr(obj, f), str) and t.strip()
        ]

    def teaser_texts(b: ProjectBundle) -> list[tuple[str, str]]:
        return [(f"teaser {n}: alt", t.alt) for n, t in (b.teasers.teasers.items() if b.teasers else [])]

    def run_guard(policy: str, texts: list[tuple[str, str]], *, cached_only: bool) -> list[TextCheck]:
        out: list[TextCheck] = []
        for label, text in texts:
            key = (policy, text)
            if key not in guarded:
                if cached_only or not can_guard:
                    continue
                guarded[key] = darkroom.check_text(text, policy=policy)
            passed, detail = guarded[key]
            out.append(TextCheck(field=label, passed=passed, detail=detail))
        return out

    def checks(
        b: ProjectBundle, texts: list[tuple[str, str]], strings: list[str], *, cached_only: bool
    ) -> ProjectChecks:
        try:
            found = problems(b, frame_keys())
        except HTTPException as e:  # a broken frames directory: report it, don't hide the project's own
            found = [f"frames can't be listed: {e.detail}"]
        return ProjectChecks(
            problems=found,
            spoiler_rules=spoilers.hits(*strings),
            guard=run_guard(b.project.policy, texts, cached_only=cached_only),
            guard_ran=can_guard,
        )

    def all_texts(b: ProjectBundle) -> tuple[list[tuple[str, str]], list[str]]:
        texts: list[tuple[str, str]] = []
        strings: list[str] = []
        for kind in KINDS:
            for obj, _ in table(b, kind).values():
                texts += guard_texts(kind, obj)
                strings += _strings(_doc(obj))
        texts += teaser_texts(b)
        strings += _strings(b.teasers.model_dump(mode="json")) if b.teasers else []
        return texts, strings

    @app.get("/projects")
    def list_projects() -> ProjectList:
        out: list[ProjectSummary] = []
        if projects_dir.is_dir():
            for d in sorted(p for p in projects_dir.iterdir() if p.is_dir() and _ID.fullmatch(p.name)):
                if not (d / "project.yaml").is_file():
                    continue
                try:
                    p = load_project(d / "project.yaml")
                except ProjectError:
                    log.warning("project_invalid", project=d.name)
                    continue
                out.append(ProjectSummary(id=d.name, title=p.title, status=p.status))
        return ProjectList(projects=out)

    @app.get("/projects/{pid}")
    def get_project(pid: str) -> ProjectDetail:
        b = bundle(pid)
        files = [project_file(b, kind, obj, path) for kind in KINDS for obj, path in table(b, kind).values()]
        texts, strings = all_texts(b)
        return ProjectDetail(id=pid, files=files, teasers=b.teasers, checks=checks(b, texts, strings, cached_only=True))

    @app.post("/projects/{pid}/files/{kind}/{item_id}/{version}")
    def save_file(
        pid: str,
        kind: str,
        item_id: str,
        version: int,
        body: SaveProjectFile,
        authorization: Annotated[str | None, Header()] = None,
    ) -> SavedProjectFile:
        require_token(authorization)
        if kind not in KINDS:
            raise HTTPException(404, f"no project file kind {kind!r}")
        root = project_root(pid)
        if not _ID.fullmatch(item_id) or version < 1:
            raise HTTPException(422, "an id is lower-case letters, digits and hyphens, and a version is 1 or more")
        if kind == "project" and item_id != pid:
            raise HTTPException(422, f"project.yaml's id is the project's own ({pid})")
        given = [k for k in _REVIEW_KEYS if k in body.doc]
        if given:
            raise HTTPException(422, f"a saved file is a draft: leave out {', '.join(given)}")
        for key, want in (("id", item_id), ("version", version), *((("project", pid),) if kind != "project" else ())):
            if key in body.doc and body.doc[key] != want:
                raise HTTPException(422, f"`{key}` is {body.doc[key]!r} in the file but {want!r} in the path")
        doc = {"id": item_id, "version": version, "status": "draft"}
        if kind != "project":
            doc["project"] = pid
        doc |= {k: v for k, v in body.doc.items() if k not in doc}
        spec = KINDS[kind]
        try:
            obj = spec.model.model_validate(doc)
        except ValidationError as e:
            raise HTTPException(422, f"{kind} {item_id} v{version}: {e}") from e

        if kind != "project" and not (root / "project.yaml").is_file():
            raise HTTPException(404, f"no project {pid}: save its project.yaml first")
        with lock:
            existing = None
            if (root / "project.yaml").is_file():
                existing = table(bundle(pid), kind).get((item_id, version))
            if existing is not None and existing[0].status != "draft":
                raise HTTPException(409, f"{kind} {item_id} v{version} is {existing[0].status}; save a new version")
            if existing is not None:
                path = existing[1]
            elif spec.subdir is None:
                path = root / "project.yaml"
            else:
                path = root / spec.subdir / f"{item_id}-v{version}.yaml"
            if path.exists() and not os.access(path, os.W_OK):
                raise HTTPException(503, f"{path.relative_to(projects_dir)} is not writable on this server")
            original = path.read_text(encoding="utf-8") if path.exists() else None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(_doc(obj), sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")
            try:
                b = load_bundle(root)
            except ProjectError as e:
                if original is None:
                    path.unlink()
                else:
                    path.write_text(original, encoding="utf-8")
                raise HTTPException(422, f"the project wouldn't load with this file, so it wasn't saved: {e}") from e
        saved_obj, saved_path = table(b, kind)[(item_id, version)]
        result = checks(b, guard_texts(kind, saved_obj), _strings(_doc(saved_obj)), cached_only=False)
        return SavedProjectFile(file=project_file(b, kind, saved_obj, saved_path), checks=result)

    @app.post("/projects/{pid}/teasers")
    def save_teasers(
        pid: str, body: SaveTeasers, authorization: Annotated[str | None, Header()] = None
    ) -> SavedProjectFile:
        require_token(authorization)
        root = project_root(pid)
        if body.teasers.project != pid:
            raise HTTPException(422, f"teasers.json is for project {body.teasers.project!r}, not {pid!r}")
        with lock:
            bundle(pid)  # the project exists and loads
            path = root / "teasers.json"
            original = path.read_text(encoding="utf-8") if path.exists() else None
            path.write_text(
                json.dumps(body.teasers.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            try:
                b = load_bundle(root)
            except ProjectError as e:  # pragma: no cover - the body was already validated as TeaserLinks
                if original is None:
                    path.unlink()
                else:
                    path.write_text(original, encoding="utf-8")
                raise HTTPException(422, str(e)) from e
        strings = _strings(b.teasers.model_dump(mode="json")) if b.teasers else []
        return SavedProjectFile(file=None, checks=checks(b, teaser_texts(b), strings, cached_only=False))

    @app.post("/projects/{pid}/files/{kind}/{item_id}/{version}/review")
    def review_file(
        pid: str,
        kind: str,
        item_id: str,
        version: int,
        body: CatalogueDecision,
        authorization: Annotated[str | None, Header()] = None,
    ) -> ProjectFile:
        require_token(authorization)
        if kind not in KINDS:
            raise HTTPException(404, f"no project file kind {kind!r}")
        b = bundle(pid)
        found = table(b, kind).get((item_id, version))
        if found is None:
            raise HTTPException(404, f"no {kind} {item_id} v{version} in project {pid}")
        obj, path = found
        if not os.access(path, os.W_OK):
            raise HTTPException(503, f"{path.relative_to(projects_dir)} is not writable on this server")
        if body.decision == "approve":
            hits = spoilers.hits(*_strings(_doc(obj)))
            if hits:
                raise HTTPException(409, "held by spoiler rule " + ", ".join(map(str, hits)))
            if not can_guard:
                raise HTTPException(
                    409, "no darkroom is configured, so SingGuard can't check this text and it can't be approved"
                )
            failed = [c for c in run_guard(b.project.policy, guard_texts(kind, obj), cached_only=False) if not c.passed]
            if failed:
                raise HTTPException(409, "SingGuard refused: " + "; ".join(f"{c.field} ({c.detail})" for c in failed))
        with lock:
            try:
                record_decision(
                    path, body.decision, by=body.by, today=datetime.now(UTC).date(), validate=KINDS[kind].loader
                )
            except CatalogueError as e:
                raise HTTPException(409, str(e)) from e
        b = bundle(pid)
        obj, path = table(b, kind)[(item_id, version)]
        return project_file(b, kind, obj, path)
