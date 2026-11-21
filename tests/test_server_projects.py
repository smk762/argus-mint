"""Teaser project routes: authoring draft files, the checks on save, and review."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from argus_mint.server import Settings, create_app

REPO = Path(__file__).resolve().parents[1]
TOKEN = "s3cret-moderator"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PID = "example-teasers"


class Guard:
    """Stands in for darkroom's text pre-flight (SingGuard): refuses any text containing ``refuse``."""

    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    def check_text(self, text, policy=None):
        self.calls.append((text, policy))
        return ("refuse" not in text), ("rule-7" if "refuse" in text else "")


@pytest.fixture
def make(tmp_path):
    projects = tmp_path / "projects"
    shutil.copytree(REPO / "projects" / PID, projects / PID)
    spoilers = tmp_path / "spoilers.txt"
    spoilers.write_text("# private\nbrass lens\n")

    def build(darkroom=None, *, guard=True, **over):
        settings = {
            "drafts_dir": tmp_path / "drafts",
            "samples_dir": tmp_path / "samples",
            "templates_dir": REPO / "templates",
            "frames_dir": REPO / "frames",
            "bios_dir": REPO / "bios",
            "projects_dir": projects,
            "spoilers": spoilers,
            "moderator_token": TOKEN,
        }
        cfg = Settings(**(settings | over))
        g = darkroom or (Guard() if guard else None)
        return TestClient(create_app(cfg, darkroom=g, probe_fn=lambda url: False)), g, projects / PID

    return build


def _save(c, kind, item, version, doc, auth=AUTH):
    return c.post(f"/projects/{PID}/files/{kind}/{item}/{version}", json={"doc": doc}, headers=auth)


def _approve(c, kind, item, version):
    return c.post(
        f"/projects/{PID}/files/{kind}/{item}/{version}/review",
        json={"decision": "approve", "by": "author"},
        headers=AUTH,
    )


LINE = {"voice": "keeper", "text": "The tide keeps no appointments.", "source": "lines.md"}


def test_capability_and_listing(make):
    c, _, _ = make()
    assert c.get("/health").json()["capabilities"]["author_catalogue"] is True
    assert c.get("/projects").json() == {"projects": [{"id": PID, "title": "Lantern Coast", "status": "draft"}]}
    detail = c.get(f"/projects/{PID}").json()
    kinds = sorted((f["kind"], f["id"]) for f in detail["files"])
    assert ("identity", "keeper") in kinds and ("project", PID) in kinds
    assert ("teasers", PID) in kinds
    links = next(f for f in detail["files"] if f["kind"] == "teasers")
    assert links["file"] == f"teasers/{PID}-v1.json" and set(links["doc"]["teasers"]) == {"T1", "T2"}
    assert detail["checks"]["problems"] == []


def test_example_project_trips_the_spoiler_rule_by_number_only(make):
    c, _, _ = make()
    checks = c.get(f"/projects/{PID}").json()["checks"]
    assert checks["spoiler_rules"] == [1]  # the lamp-room scene mentions the brass lens
    assert "brass" not in json.dumps(checks)


def test_save_writes_a_draft_and_checks_it(make):
    c, guard, root = make()
    r = _save(c, "line", "tide", 1, LINE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file"]["status"] == "draft" and body["file"]["file"] == "lines/tide-v1.yaml"
    assert body["checks"]["guard"] == [{"field": "line tide v1: text", "passed": True, "detail": ""}]
    assert guard.calls == [(LINE["text"], "default")]  # under the project's policy
    on_disk = yaml.safe_load((root / "lines" / "tide-v1.yaml").read_text())
    assert on_disk["status"] == "draft" and on_disk["project"] == PID
    # the verdict is kept in the file: field, the text's sha256, the policy, the outcome
    [v] = on_disk["guard"]
    assert v["field"] == "text" and v["policy"] == "default" and v["passed"] is True
    assert v["sha256"] == hashlib.sha256(LINE["text"].encode()).hexdigest()


def test_save_needs_the_token_and_refuses_review_fields(make):
    c, _, _ = make()
    assert _save(c, "line", "tide", 1, LINE, auth={}).status_code == 401
    r = _save(c, "line", "tide", 1, {**LINE, "status": "approved"})
    assert r.status_code == 422 and "draft" in r.json()["detail"]
    forged = [
        {"field": "text", "sha256": "0" * 64, "policy": "default", "passed": True, "checked_at": "2026-11-18T00:00:00Z"}
    ]
    r = _save(c, "line", "tide", 1, {**LINE, "guard": forged})  # verdicts are SingGuard's, never a client's
    assert r.status_code == 422 and "guard" in r.json()["detail"]


def test_save_refuses_a_mismatch_with_the_path(make):
    c, _, _ = make()
    assert _save(c, "line", "tide", 1, {**LINE, "id": "other"}).status_code == 422
    assert _save(c, "line", "tide", 1, {**LINE, "project": "elsewhere"}).status_code == 422
    assert _save(c, "project", "elsewhere", 1, {"title": "x"}).status_code == 422
    assert _save(c, "nonsense", "tide", 1, LINE).status_code == 404


def test_invalid_file_is_refused_and_nothing_is_written(make):
    c, _, root = make()
    r = _save(c, "identity", "ghost", 1, {"voice": "keeper", "presence": "embodied"})  # no prompt
    assert r.status_code == 422 and "needs a prompt" in r.json()["detail"]
    assert not (root / "identities" / "ghost-v1.yaml").exists()


def test_approved_files_never_change(make):
    c, _, _ = make()
    assert _save(c, "line", "tide", 1, LINE).status_code == 200
    assert _approve(c, "line", "tide", 1).status_code == 200
    r = _save(c, "line", "tide", 1, {**LINE, "text": "Changed."})
    assert r.status_code == 409 and "new version" in r.json()["detail"]
    assert _save(c, "line", "tide", 2, {**LINE, "text": "Changed."}).status_code == 200


def test_approval_records_the_reviewer(make):
    c, _, root = make()
    _save(c, "line", "tide", 1, LINE)
    f = _approve(c, "line", "tide", 1).json()
    assert f["status"] == "approved" and f["doc"]["approved_by"] == "author"


def test_spoiler_holds_approval_by_rule_number(make):
    c, _, _ = make()
    r = _approve(c, "scene", "lamp-room", 1)
    assert r.status_code == 409 and r.json()["detail"] == "held by spoiler rule 1"


def test_singguard_refusal_holds_approval(make):
    c, _, _ = make()
    r = _save(c, "line", "bad", 1, {**LINE, "text": "please refuse this"})
    assert r.json()["checks"]["guard"][0] == {"field": "line bad v1: text", "passed": False, "detail": "rule-7"}
    r = _approve(c, "line", "bad", 1)
    assert r.status_code == 409 and "SingGuard refused" in r.json()["detail"]


def test_no_darkroom_means_no_approval(make):
    c, _, _ = make(guard=False)
    assert c.get(f"/projects/{PID}").json()["checks"]["guard_ran"] is False
    r = _approve(c, "line", "every-boat", 1)
    assert r.status_code == 409 and "SingGuard" in r.json()["detail"]


def test_a_new_project_starts_from_its_project_yaml(make):
    c, _, _ = make()
    assert c.post("/projects/new-one/files/line/a/1", json={"doc": LINE}, headers=AUTH).status_code == 404
    r = c.post("/projects/new-one/files/project/new-one/1", json={"doc": {"title": "New"}}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert [p["id"] for p in c.get("/projects").json()["projects"]] == ["example-teasers", "new-one"]


def test_project_ids_are_checked_before_any_path_is_built(make):
    c, _, _ = make()
    assert c.get(f"/projects/{PID}").status_code == 200  # a valid id reaches the handler
    # Percent-encoded, so the client can't drop the dot segment and send GET / instead.
    for bad in ("%2E%2E", "Bad_Id"):
        r = c.get(f"/projects/{bad}")
        # The validator's message quotes the id (repr); the later "no project.yaml" one doesn't.
        assert r.status_code == 404 and r.json()["detail"] in ("no project '..'", "no project 'Bad_Id'"), r.text


def _links(root: Path, version: int = 1) -> dict:
    doc = json.loads((root / "teasers" / f"{PID}-v{version}.json").read_text())
    drop = ("id", "version", "project", "status", "approved_by", "approved_at", "guard")
    return {k: v for k, v in doc.items() if k not in drop}


def test_save_teasers_checks_alt_text_and_cross_file_problems(make):
    c, _, root = make()
    doc = _links(root)
    doc["teasers"]["T2"]["alt"] = "please refuse"
    doc["teasers"]["T1"]["line"] = {"id": "every-boat", "version": 1}  # wrong voice
    r = _save(c, "teasers", PID, 2, doc)
    assert r.status_code == 200, r.text
    assert r.json()["file"]["file"] == f"teasers/{PID}-v2.json"
    checks = r.json()["checks"]
    assert any("voice" in p for p in checks["problems"])
    assert {"field": f"teasers {PID} v2: T2.alt", "passed": False, "detail": "rule-7"} in checks["guard"]
    assert _save(c, "teasers", "elsewhere", 3, doc).status_code == 422  # links take the project's id
    assert _save(c, "teasers", PID, 3, {**doc, "project": "elsewhere"}).status_code == 422


def _off_the_spoiler_list(root):
    for f in (root / "scenes" / "lamp-room-v1.yaml", root / "teasers" / f"{PID}-v1.json"):
        f.write_text(f.read_text().replace("brass lens", "great lens"))


def _approve_everything_the_links_name(c):
    for kind, item in (("line", "keep-it-lit"), ("line", "every-boat"), ("identity", "keeper"), ("identity", "lamp"),
                       ("scene", "lamp-room"), ("scene", "shoreline")):  # fmt: skip
        r = _approve(c, kind, item, 1)
        assert r.status_code == 200, (kind, item, r.text)


def test_teaser_links_are_reviewed_and_only_link_approved_files(make):
    c, _, root = make()
    r = _approve(c, "teasers", PID, 1)
    assert r.status_code == 409 and r.json()["detail"] == "held by spoiler rule 1"  # T1's alt names the lens
    _off_the_spoiler_list(root)
    r = _approve(c, "teasers", PID, 1)
    assert r.status_code == 409 and "aren't approved" in r.json()["detail"]
    assert "line keep-it-lit v1" in r.json()["detail"]
    _approve_everything_the_links_name(c)
    r = _approve(c, "teasers", PID, 1)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved" and r.json()["doc"]["approved_by"] == "author"
    stored = json.loads((root / "teasers" / f"{PID}-v1.json").read_text())
    assert {v["field"] for v in stored["guard"]} == {"T1.alt", "T2.alt"}  # verdicts kept in the file
    r = _save(c, "teasers", PID, 1, _links(root))
    assert r.status_code == 409 and "new version" in r.json()["detail"]


def test_stored_verdicts_outlive_the_server_and_an_edit_drops_them(make):
    c, guard, _ = make()
    _save(c, "line", "tide", 1, LINE)
    assert guard.calls == [(LINE["text"], "default")]
    c2, guard2, _ = make()  # a restart: a new app, nothing in memory
    assert {"field": "line tide v1: text", "passed": True, "detail": ""} in c2.get(f"/projects/{PID}").json()["checks"][
        "guard"
    ]
    assert _approve(c2, "line", "tide", 1).status_code == 200
    assert guard2.calls == []  # the stored verdict stood: SingGuard wasn't asked again
    _save(c2, "line", "tide", 2, {**LINE, "text": "A changed line."})
    assert guard2.calls == [("A changed line.", "default")]


def test_a_stored_pass_approves_even_without_a_darkroom(make):
    c, _, _ = make()
    _save(c, "line", "tide", 1, LINE)
    offline, _, _ = make(guard=False)
    assert _approve(offline, "line", "tide", 1).status_code == 200


def test_approval_judges_a_hand_written_file_and_keeps_the_verdict(make):
    c, guard, root = make()
    path = root / "lines" / "every-boat-v1.yaml"
    path.write_text("# the lamp speaks\n" + path.read_text())
    assert "guard:" not in path.read_text()
    assert _approve(c, "line", "every-boat", 1).status_code == 200
    assert len(guard.calls) == 1
    text = path.read_text()
    assert "guard:" in text and text.startswith("#")  # the author's comments are kept


def test_unreadable_spoiler_file_fails_at_start(make, tmp_path):
    with pytest.raises(Exception, match="spoiler file"):
        make(spoilers=tmp_path / "missing.txt")


def test_read_only_server_does_not_author(make):
    c, _, _ = make(read_only=True)
    assert c.get("/health").json()["capabilities"]["author_catalogue"] is False
    assert _save(c, "line", "tide", 1, LINE).status_code == 403


def test_an_identity_with_an_unconsented_reference_is_not_approved(make, tmp_path):
    from tests.test_references import CONSENTED, IMAGE, RELEASE, SUBJECT, _record

    consent_dir = tmp_path / "consent"
    consent_dir.mkdir()
    c, _, _ = make(consent_dir=consent_dir)
    doc = {
        "voice": "keeper",
        "presence": "embodied",
        "prompt": "an old keeper in an oilskin coat",
        "references": [{"file": "refs/jo.png", "sha256": IMAGE, "provenance": CONSENTED}],
    }
    r = _save(c, "identity", "jo", 1, doc)
    assert r.status_code == 200, r.text
    problems = r.json()["checks"]["problems"]
    assert any("no consent record" in p for p in problems) and not any(SUBJECT in p for p in problems)
    r = _approve(c, "identity", "jo", 1)
    assert r.status_code == 409 and "references can't be approved" in r.json()["detail"]
    (consent_dir / f"{RELEASE}.json").write_text(json.dumps(_record()))
    assert _approve(c, "identity", "jo", 1).status_code == 200
