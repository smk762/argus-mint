"""HTTP routes for teaser projects: list, read, author (draft files only), check and review.

Authoring is the one place prompt text reaches argus-mint from a client, so it is held tight:

- only the moderator writes, and only ``draft`` files (an approved or retired file never
  changes; the author saves a new version);
- every save is checked: the file's schema, the project's cross-file problems, the private
  spoiler list (reported by rule number only) and SingGuard, through darkroom's text pre-flight
  under the project's own policy;
- SingGuard's verdicts are stored in the file itself (its ``guard`` block, left out of its hash).
  A verdict counts while the text and the policy are the ones it judged, so a restart keeps it
  and an edit or a new policy drops it;
- approval is refused while a spoiler rule matches, or while any of the file's texts lacks a
  passing verdict; with no darkroom configured, a missing verdict can't be made, so that file
  can't be approved;
- teaser links (``teasers/<project>-v<n>.json``) are reviewed like the rest, and are approved
  only when every file they link is approved and nothing in them is wrong;
- nothing renders from a file until it is approved (hard rule 2); renders are another module's.
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
from argus_mint.catalogue import CatalogueError, record_decision, record_guard
from argus_mint.projects import (
    REVIEW_KEYS,
    GuardVerdict,
    Identity,
    Line,
    Project,
    ProjectBundle,
    ProjectError,
    Scene,
    TeaserLinks,
    file_strings,
    fresh_verdicts,
    load_bundle,
    load_identity,
    load_line,
    load_project,
    load_scene,
    load_teasers,
    problems,
    teaser_problems,
    text_sha256,
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
    TextCheck,
)

log = structlog.get_logger()

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class _Kind:
    def __init__(self, subdir: str | None, loader: Callable[[Path], Any], model: type[BaseModel], ext: str = "yaml"):
        self.subdir = subdir  # None: project.yaml at the project's root
        self.loader = loader
        self.model = model
        self.ext = ext


KINDS: dict[str, _Kind] = {
    "project": _Kind(None, load_project, Project),
    "identity": _Kind("identities", load_identity, Identity),
    "scene": _Kind("scenes", load_scene, Scene),
    "line": _Kind("lines", load_line, Line),
    "teasers": _Kind("teasers", load_teasers, TeaserLinks, ext="json"),
}
_OWN_ID = ("project", "teasers")  # kinds whose id is the project's own


def _doc(obj: BaseModel) -> dict[str, Any]:
    doc = obj.model_dump(mode="json")
    if not doc.get("guard"):
        doc.pop("guard", None)
    return doc


def _file_text(path: Path, obj: BaseModel) -> str:
    if path.suffix == ".json":
        return json.dumps(_doc(obj), indent=2, ensure_ascii=False) + "\n"
    return yaml.safe_dump(_doc(obj), sort_keys=False, allow_unicode=True, width=100)


def add_project_routes(
    app: FastAPI,
    *,
    projects_dir: Path,
    spoilers: SpoilerList,
    darkroom: Any,
    require_token: Callable[[str | None], None],
    lock: threading.Lock,
    frame_keys: Callable[[], set[tuple[str, int]]],
    vouch: Callable[[str, Identity], str | None] | None = None,
) -> Callable[[str, str, str, int, dict[str, Any]], SavedProjectFile]:
    """Add the routes; returns the save function (``save(pid, kind, id, version, doc)``) the
    route uses, so a training run's pin saves its draft identity through the same checks.

    ``vouch(pid, identity)``: why an identity's pinned LoRA may not be approved (no passing
    training run pinned it), or None.
    """
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
        return {"identity": b.identities, "scene": b.scenes, "line": b.lines, "teasers": b.teasers}[kind]

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

    def judge(texts: list[tuple[str, str]], policy: str) -> list[GuardVerdict]:
        """New verdicts on ``texts`` (darkroom's text pre-flight under ``policy``); none without a darkroom."""
        if not can_guard:
            return []
        now = datetime.now(UTC)
        out = []
        for f, text in texts:
            passed, detail = darkroom.check_text(text, policy=policy)
            out.append(
                GuardVerdict(
                    field=f, sha256=text_sha256(text), policy=policy, passed=passed, detail=detail, checked_at=now
                )
            )
        return out

    def merged(obj: Any, new: list[GuardVerdict], policy: str) -> tuple[GuardVerdict, ...]:
        """The file's verdicts that still hold, plus ``new``: stale ones are dropped."""
        fresh, _ = fresh_verdicts(obj, policy)
        by_field = {**fresh, **{v.field: v for v in new}}
        return tuple(by_field[f] for f, _ in obj.guarded_texts() if f in by_field)

    def text_checks(kind: str, obj: Any, policy: str) -> list[TextCheck]:
        fresh, _ = fresh_verdicts(obj, policy)
        label = f"{kind} {obj.id} v{obj.version}"
        return [TextCheck(field=f"{label}: {f}", passed=v.passed, detail=v.detail) for f, v in fresh.items()]

    def checks(b: ProjectBundle, files: list[tuple[str, Any]]) -> ProjectChecks:
        try:
            found = problems(b, frame_keys())
        except HTTPException as e:  # a broken frames directory: report it, don't hide the project's own
            found = [f"frames can't be listed: {e.detail}"]
        policy = b.project.policy
        return ProjectChecks(
            problems=found,
            spoiler_rules=spoilers.hits(*(s for _, obj in files for s in file_strings(_doc(obj)))),
            guard=[c for kind, obj in files for c in text_checks(kind, obj, policy)],
            guard_ran=can_guard,
        )

    def all_files(b: ProjectBundle) -> list[tuple[str, Any]]:
        return [(kind, obj) for kind in KINDS for obj, _ in table(b, kind).values()]

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
        return ProjectDetail(id=pid, files=files, checks=checks(b, all_files(b)))

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
        return save(pid, kind, item_id, version, body.doc)

    def save(pid: str, kind: str, item_id: str, version: int, given_doc: dict[str, Any]) -> SavedProjectFile:
        if kind not in KINDS:
            raise HTTPException(404, f"no project file kind {kind!r}")
        root = project_root(pid)
        if not _ID.fullmatch(item_id) or version < 1:
            raise HTTPException(422, "an id is lower-case letters, digits and hyphens, and a version is 1 or more")
        if kind in _OWN_ID and item_id != pid:
            raise HTTPException(422, f"a {kind} file's id is the project's own ({pid})")
        given = [k for k in REVIEW_KEYS if k in given_doc]
        if given:
            raise HTTPException(422, f"a saved file is a draft, checked here: leave out {', '.join(given)}")
        for key, want in (("id", item_id), ("version", version), *((("project", pid),) if kind != "project" else ())):
            if key in given_doc and given_doc[key] != want:
                raise HTTPException(422, f"`{key}` is {given_doc[key]!r} in the file but {want!r} in the path")
        doc = {"id": item_id, "version": version, "status": "draft"}
        if kind != "project":
            doc["project"] = pid
        doc |= {k: v for k, v in given_doc.items() if k not in doc}
        spec = KINDS[kind]
        try:
            obj = spec.model.model_validate(doc)
        except ValidationError as e:
            raise HTTPException(422, f"{kind} {item_id} v{version}: {e}") from e

        if kind != "project" and not (root / "project.yaml").is_file():
            raise HTTPException(404, f"no project {pid}: save its project.yaml first")
        existing = table(bundle(pid), kind).get((item_id, version)) if (root / "project.yaml").is_file() else None
        if existing is not None and existing[0].status != "draft":
            raise HTTPException(409, f"{kind} {item_id} v{version} is {existing[0].status}; save a new version")
        # SingGuard judges the texts before anything is written (and outside the lock: it's a network call).
        policy = obj.policy if kind == "project" else bundle(pid).project.policy
        obj = obj.model_copy(update={"guard": tuple(judge(obj.guarded_texts(), policy))})
        with lock:
            existing = table(bundle(pid), kind).get((item_id, version)) if (root / "project.yaml").is_file() else None
            if existing is not None and existing[0].status != "draft":
                raise HTTPException(409, f"{kind} {item_id} v{version} is {existing[0].status}; save a new version")
            if existing is not None:
                path = existing[1]
            elif spec.subdir is None:
                path = root / "project.yaml"
            else:
                path = root / spec.subdir / f"{item_id}-v{version}.{spec.ext}"
            if path.exists() and not os.access(path, os.W_OK):
                raise HTTPException(503, f"{path.relative_to(projects_dir)} is not writable on this server")
            original = path.read_text(encoding="utf-8") if path.exists() else None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_file_text(path, obj), encoding="utf-8")
            try:
                b = load_bundle(root)
            except ProjectError as e:
                if original is None:
                    path.unlink()
                else:
                    path.write_text(original, encoding="utf-8")
                raise HTTPException(422, f"the project wouldn't load with this file, so it wasn't saved: {e}") from e
        saved_obj, saved_path = table(b, kind)[(item_id, version)]
        return SavedProjectFile(
            file=project_file(b, kind, saved_obj, saved_path), checks=checks(b, [(kind, saved_obj)])
        )

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
            hits = spoilers.hits(*file_strings(_doc(obj)))
            if hits:
                raise HTTPException(409, "held by spoiler rule " + ", ".join(map(str, hits)))
            if kind == "teasers":
                wrong = teaser_problems(b, obj)
                if wrong:
                    raise HTTPException(409, "the teaser links have problems: " + "; ".join(wrong))
                unapproved = sorted(
                    {
                        f"{k} {ref.id} v{ref.version}"
                        for t in obj.teasers.values()
                        for k, ref, tab in (
                            ("line", t.line, b.lines),
                            ("identity", t.identity, b.identities),
                            ("scene", t.scene, b.scenes),
                        )  # fmt: skip
                        if ref.key not in tab or tab[ref.key][0].status != "approved"
                    }
                )
                if unapproved:
                    raise HTTPException(409, "teasers link files that aren't approved: " + ", ".join(unapproved))
            if kind == "identity" and obj.lora is not None:
                why = vouch(pid, obj) if vouch is not None else "LoRAs are pinned by a training run"
                if why:
                    raise HTTPException(409, f"the identity's LoRA can't be approved: {why}")
            policy = b.project.policy if kind != "project" else obj.policy
            _fresh, missing = fresh_verdicts(obj, policy)
            if missing and not can_guard:
                raise HTTPException(
                    409, "no darkroom is configured, so SingGuard can't check this text and it can't be approved"
                )
            new = judge(missing, policy)
            if new:  # keep them in the file, whatever they say, so the next look needn't ask again
                with lock:
                    try:
                        record_guard(path, [v.model_dump(mode="json") for v in merged(obj, new, policy)],
                                     validate=KINDS[kind].loader)  # fmt: skip
                    except CatalogueError as e:
                        raise HTTPException(409, str(e)) from e
                obj = KINDS[kind].loader(path)
            failed = [c for c in text_checks(kind, obj, policy) if not c.passed]
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

    return save
