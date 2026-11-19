"""Teaser projects (projects.py) and the private spoiler list (spoilers.py)."""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from argus_mint.catalogue import record_decision, record_guard
from argus_mint.projects import (
    GuardVerdict,
    Identity,
    Line,
    ProjectError,
    Reference,
    Scene,
    fresh_verdicts,
    load_bundle,
    load_identity,
    load_line,
    load_teasers,
    problems,
    text_sha256,
)
from argus_mint.spoilers import SpoilerError, load_spoilers, parse

EXAMPLE = Path(__file__).resolve().parents[1] / "projects" / "example-teasers"
FRAMES = {("example-teaser", 1), ("example-teasers/harbour-feed", 1)}
SAMPLING = {"sampler": "euler", "scheduler": "normal", "steps": 20, "cfg": 6.0}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "example-teasers"
    shutil.copytree(EXAMPLE, root)
    return root


TEASERS = Path("teasers") / "example-teasers-v1.json"


def _teasers(root: Path) -> dict:
    return json.loads((root / TEASERS).read_text())


def _write_teasers(root: Path, doc: dict) -> None:
    (root / TEASERS).write_text(json.dumps(doc))


# -- the example ----------------------------------------------------------------------------------


def test_example_project_loads_without_problems() -> None:
    b = load_bundle(EXAMPLE)
    assert b.id == b.project.id == "example-teasers"
    assert {k[0] for k in b.identities} == {"keeper", "lamp"}
    assert {k[0] for k in b.scenes} == {"lamp-room", "shoreline"}
    links, _ = b.teasers[("example-teasers", 1)]
    assert set(links.teasers) == {"T1", "T2"} and links.status == "draft"
    assert b.current_teasers() is None  # nothing approved yet
    assert problems(b, FRAMES) == []


def test_unknown_frame_is_reported_when_the_server_lists_its_frames() -> None:
    found = problems(load_bundle(EXAMPLE), frames=set())
    assert any("frame example-teaser v1" in p for p in found)


# -- each file on its own --------------------------------------------------------------------------


def test_line_keeps_the_verbatim_text_and_shows_the_published_one() -> None:
    line = load_line(EXAMPLE / "lines" / "keep-it-lit-v1.yaml")
    assert line.text.startswith("Fourty")  # as written, typo and all
    assert line.shown.startswith("Forty")  # as published
    plain = load_line(EXAMPLE / "lines" / "every-boat-v1.yaml")
    assert plain.shown == plain.text


def test_approval_is_recorded_before_any_other_status() -> None:
    with pytest.raises(ValueError, match="approved_by"):
        Line(id="x", version=1, status="approved", project="p", voice="v", text="t", source="s")


def test_embodied_identity_needs_a_prompt_and_no_frame() -> None:
    base = {"id": "a", "version": 1, "status": "draft", "project": "p", "voice": "v", "presence": "embodied"}
    with pytest.raises(ValueError, match="needs a prompt"):
        Identity(**base)
    with pytest.raises(ValueError, match="no frame"):
        Identity(**base, prompt="someone", frame={"id": "f", "version": 1})
    with pytest.raises(ValueError, match="fixed text"):
        Identity(**base, prompt="a {role}")


def test_viewpoint_identity_needs_its_frame_and_takes_no_lora() -> None:
    base = {"id": "a", "version": 1, "status": "draft", "project": "p", "voice": "v", "presence": "viewpoint"}
    with pytest.raises(ValueError, match="needs its frame"):
        Identity(**base)
    with pytest.raises(ValueError, match="no LoRA"):
        Identity(
            **base, frame={"id": "f", "version": 1}, lora={"name": "x.safetensors"}, proof_run="r1", proof_export="e"
        )


def test_lora_comes_with_the_proof_run_that_passed_it_and_its_references() -> None:
    base = {"id": "a", "version": 1, "status": "draft", "project": "p", "voice": "v", "presence": "embodied"}
    for partial in (
        {"lora": {"name": "x.safetensors"}},
        {"lora": {"name": "x.safetensors"}, "proof_run": "r1"},
        {"proof_run": "r1", "proof_export": "e"},
        {"proof_export": "e"},
    ):
        with pytest.raises(ValueError, match="go together"):
            Identity(**base, prompt="someone", **partial)
    pinned = Identity(**base, prompt="someone", lora={"name": "x.safetensors"}, proof_run="r1", proof_export="e")
    assert (pinned.proof_run, pinned.proof_export) == ("r1", "e")


def test_fields_added_for_training_leave_an_identitys_hash_as_it_was_while_unset() -> None:
    base = {"id": "a", "version": 1, "status": "draft", "project": "p", "voice": "v", "presence": "embodied"}
    plain = Identity(**base, prompt="someone")
    old = {k: v for k, v in plain.model_dump(mode="json").items()
           if k not in ("status", "guard", *Identity.UNHASHED_WHEN_UNSET)}  # fmt: skip
    from argus_mint._codec import canonical, keccak256

    assert plain.hash() == keccak256(canonical(old))
    assert Identity(**base, prompt="someone", check_metric="identity").hash() != plain.hash()


def test_training_settings_are_checked() -> None:
    base = {"id": "a", "version": 1, "status": "draft", "project": "p", "voice": "v"}
    sampling = {"sampler": "euler", "scheduler": "normal", "steps": 20, "cfg": 5}
    training = {"model": "sdxl/x.safetensors", "sampling": sampling, "trigger": "kpr_keeper"}
    ident = Identity(**base, presence="embodied", prompt="someone", training=training)
    assert ident.training is not None and (ident.training.candidates, ident.training.train) == (60, 30)
    with pytest.raises(ValueError, match="need more than"):
        Identity(**base, presence="embodied", prompt="x", training={**training, "candidates": 20, "train": 15})
    with pytest.raises(ValueError, match="trigger"):
        Identity(**base, presence="embodied", prompt="x", training={**training, "trigger": "Has Space"})
    with pytest.raises(ValueError, match="nothing is trained"):
        Identity(**base, presence="viewpoint", frame={"id": "f", "version": 1}, training=training)


def test_a_reference_carries_a_provenance_record() -> None:
    sha = "ab" * 32
    with pytest.raises(ValueError, match="kind"):
        Reference(file="a.png", sha256=sha, provenance={"source": "somewhere"})
    r = Reference(file="a.png", sha256=sha, provenance={"kind": "stock", "source": "s", "license": "CC0"})
    assert r.provenance["kind"] == "stock"  # checked against cortex's Provenance by the server (references.py)


def test_scene_character_area_must_fit_the_render() -> None:
    base = {
        "id": "s",
        "version": 1,
        "status": "draft",
        "project": "p",
        "model": "sdxl/m.safetensors",
        "sampling": SAMPLING,
        "width": 1024,
        "height": 1024,
        "prompt": "a room",
    }
    Scene(**base, character_area={"x": 0, "y": 0, "width": 1024, "height": 1024})
    with pytest.raises(ValueError, match="outside"):
        Scene(**base, character_area={"x": 600, "y": 0, "width": 500, "height": 100})
    with pytest.raises(ValueError, match="fixed text"):
        Scene(**{**base, "prompt": "a {place}"})


def test_hash_leaves_status_out(project: Path) -> None:
    path = project / "lines" / "every-boat-v1.yaml"
    before = load_line(path).hash()
    record_decision(path, "approve", by="author", today=date(2026, 11, 18), validate=load_line)
    approved = load_line(path)
    assert approved.status == "approved" and approved.hash() != before  # approval lines are part of it
    record_decision(path, "retire", by="author", today=date(2026, 11, 19), validate=load_line)
    assert load_line(path).hash() == approved.hash()


def test_catalogue_decisions_apply_to_project_files(project: Path) -> None:
    path = project / "identities" / "keeper-v1.yaml"
    assert record_decision(path, "approve", by="author", today=date(2026, 11, 18), validate=load_identity) == "approved"
    assert "# Embodied" in path.read_text()  # comments survive


# -- across files ---------------------------------------------------------------------------------


def test_teaser_must_name_files_that_exist(project: Path) -> None:
    doc = _teasers(project)
    doc["teasers"]["T1"]["line"] = {"id": "missing", "version": 1}
    _write_teasers(project, doc)
    assert "example-teasers-v1.json: T1: no line missing v1" in problems(load_bundle(project), FRAMES)


def test_line_voice_must_match_identity(project: Path) -> None:
    doc = _teasers(project)
    doc["teasers"]["T1"]["line"] = {"id": "every-boat", "version": 1}  # the lamp's line on the keeper
    _write_teasers(project, doc)
    assert any("voice" in p for p in problems(load_bundle(project), FRAMES))


def test_embodied_teaser_needs_a_scene_with_room_for_them(project: Path) -> None:
    doc = _teasers(project)
    doc["teasers"]["T1"]["scene"] = {"id": "shoreline", "version": 1}
    _write_teasers(project, doc)
    assert any("no character_area" in p for p in problems(load_bundle(project), FRAMES))


def test_frame_must_be_one_of_the_projects(project: Path) -> None:
    doc = _teasers(project)
    doc["teasers"]["T1"]["frame"] = {"id": "card-green", "version": 1}
    _write_teasers(project, doc)
    assert any("isn't one of the project's frames" in p for p in problems(load_bundle(project), None))


def test_files_of_another_project_are_reported(project: Path) -> None:
    path = project / "lines" / "every-boat-v1.yaml"
    path.write_text(path.read_text().replace("project: example-teasers", "project: elsewhere"))
    assert any("belongs to project 'elsewhere'" in p for p in problems(load_bundle(project), FRAMES))


def test_scene_outside_the_projects_family_is_reported(project: Path) -> None:
    path = project / "scenes" / "shoreline-v1.yaml"
    path.write_text(path.read_text().replace("model: sdxl/", "model: flux/"))
    assert any("family 'sdxl'" in p for p in problems(load_bundle(project), FRAMES))


def test_teaser_numbers_are_t_and_a_number(project: Path) -> None:
    doc = _teasers(project)
    doc["teasers"]["teaser-1"] = doc["teasers"].pop("T1")
    _write_teasers(project, doc)
    with pytest.raises(ProjectError, match="T1, T2"):
        load_bundle(project)


def test_duplicate_versions_are_refused(project: Path) -> None:
    shutil.copy(project / "lines" / "every-boat-v1.yaml", project / "lines" / "copy.yaml")
    with pytest.raises(ProjectError, match="duplicate line every-boat v1"):
        load_bundle(project)


def test_a_project_needs_its_project_yaml(tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match="no project.yaml"):
        load_bundle(tmp_path)


# -- spoilers -------------------------------------------------------------------------------------


def test_spoiler_phrases_match_whole_words_ignoring_case_and_spacing() -> None:
    rules = parse("# comment\n\nthe  Old Keeper\nre:\\bbrass\\s+lens\n")
    assert len(rules) == 2
    assert rules.hits("Nobody knows THE old\nkeeper.") == [1]
    assert rules.hits("the old keepers") == []  # a whole phrase, not a prefix
    assert rules.hits("a brass  lens", "the old keeper") == [1, 2]


def test_a_hit_is_reported_by_number_only() -> None:
    rules = parse("secret name\n")
    assert rules.hits("the secret name appears") == [1]


def test_bad_regex_is_named_by_number_not_by_text() -> None:
    with pytest.raises(SpoilerError) as e:
        parse("fine\nre:(unclosed secret\n")
    assert "rule 2" in str(e.value) and "secret" not in str(e.value)


def test_no_spoiler_file_means_no_rules_but_a_missing_one_is_an_error(tmp_path: Path) -> None:
    assert load_spoilers(None).hits("anything") == []
    with pytest.raises(SpoilerError):
        load_spoilers(tmp_path / "nope.txt")


# -- teaser links are reviewed; SingGuard verdicts live in each file -------------------------------


def test_a_lone_teasers_json_is_refused_with_the_way_forward(project: Path) -> None:
    (project / "teasers.json").write_text("{}")
    with pytest.raises(ProjectError, match="teasers/example-teasers-v1.json"):
        load_bundle(project)


def test_teaser_links_take_the_projects_id(project: Path) -> None:
    doc = _teasers(project) | {"id": "other"}
    _write_teasers(project, doc)
    with pytest.raises(ProjectError, match="project's id"):
        load_bundle(project)


def test_teaser_links_are_approved_like_any_reviewed_file(project: Path) -> None:
    path = project / TEASERS
    record_decision(path, "approve", by="author", today=date(2026, 11, 18), validate=load_teasers)
    links = load_teasers(path)
    assert links.status == "approved" and links.approved_by == "author"
    assert load_bundle(project).current_teasers() == links
    with pytest.raises(Exception, match="only a draft"):
        record_decision(path, "approve", by="author", today=date(2026, 11, 18), validate=load_teasers)


def _verdict(field: str, text: str, policy: str = "default", passed: bool = True) -> GuardVerdict:
    return GuardVerdict(
        field=field, sha256=text_sha256(text), policy=policy, passed=passed, checked_at="2026-11-18T00:00:00Z"
    )


def test_a_stored_verdict_holds_only_for_the_text_and_policy_it_judged() -> None:
    base = {"id": "tide", "version": 1, "status": "draft", "project": "p", "voice": "v", "source": "s"}
    line = Line(
        **base, text="The tide keeps no appointments.", guard=(_verdict("text", "The tide keeps no appointments."),)
    )
    fresh, missing = fresh_verdicts(line, "default")
    assert list(fresh) == ["text"] and missing == []
    assert fresh_verdicts(line, "adult")[1] == [("text", line.text)]  # another policy: judge again
    edited = line.model_copy(update={"text": "Changed."})
    assert fresh_verdicts(edited, "default")[1] == [("text", "Changed.")]  # another text: judge again
    assert line.hash() == line.model_copy(update={"guard": ()}).hash()  # verdicts aren't hashed


def test_record_guard_keeps_the_rest_of_a_yaml_file(project: Path) -> None:
    path = project / "lines" / "keep-it-lit-v1.yaml"
    before = path.read_text()
    line = load_line(path)
    v = [_verdict("text", line.text).model_dump(mode="json")]
    record_guard(path, v, validate=load_line)
    record_guard(path, v, validate=load_line)  # replaces the block, doesn't add another
    after = path.read_text()
    assert after.startswith(before.rstrip("\n")) and after.count("guard:") == 1
    assert load_line(path).guard[0].field == "text"
    record_guard(path, [], validate=load_line)
    assert path.read_text().rstrip("\n") == before.rstrip("\n")
