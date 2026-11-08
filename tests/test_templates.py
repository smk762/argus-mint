from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from argus_mint.templates import (
    NotApprovedError,
    Template,
    TemplateError,
    fill,
    load_registry,
    load_template,
    placeholders,
)

VALUES = {"role": "ranger", "colour": "crimson", "pose": "on guard"}
REPO_TEMPLATES = Path(__file__).parent.parent / "templates"


def test_fill_builds_the_prompt(template_doc):
    req = fill(Template.model_validate(template_doc), VALUES, seed=81234)
    assert req.prompt == "portrait of a ranger warrior, crimson heraldry, on guard"
    assert req.negative == "text, watermark"
    assert (req.template_id, req.template_version, req.seed) == ("test-portrait", 1, 81234)
    assert req.variables == VALUES


def test_fill_is_reproducible(template_doc):
    t = Template.model_validate(template_doc)
    assert fill(t, VALUES, 7) == fill(t, dict(reversed(VALUES.items())), 7)
    assert fill(t, VALUES, 7).model_dump_json() == fill(t, VALUES, 7).model_dump_json()


def test_value_outside_list_rejected(template_doc):
    t = Template.model_validate(template_doc)
    with pytest.raises(TemplateError, match="not in the template's list"):
        fill(t, {**VALUES, "role": "a dragon riding a unicorn"}, 1)


def test_missing_and_extra_variables_rejected(template_doc):
    t = Template.model_validate(template_doc)
    with pytest.raises(TemplateError, match="missing"):
        fill(t, {"role": "ranger", "colour": "none"}, 1)
    with pytest.raises(TemplateError, match="unknown"):
        fill(t, {**VALUES, "caption": "hello"}, 1)


@pytest.mark.parametrize("status", ["draft", "retired"])
def test_non_approved_rejected(template_doc, status):
    t = Template.model_validate({**template_doc, "status": status})
    with pytest.raises(NotApprovedError):
        fill(t, VALUES, 1)


@pytest.mark.parametrize("seed", [-1, 2**32, True, 1.5])
def test_bad_seed_rejected(template_doc, seed):
    with pytest.raises(TemplateError, match="seed"):
        fill(Template.model_validate(template_doc), VALUES, seed)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"prompt": "a {role} with {weapon}"}, "without a variable list"),
        ({"prompt": "a {role}"}, "not used in the prompt"),
        ({"prompt": "a {role} {role} {colour} {pose}"}, "more than once"),
        ({"prompt": "a {role!r} {colour} {pose}"}, "bare name"),
        ({"prompt": "a {role:>10} {colour} {pose}"}, "bare name"),
        ({"negative": "no {colour}"}, "negative"),
        ({"approved_by": None}, "approved_by"),
        ({"variables": {"role": [], "colour": ["x"], "pose": ["y"]}}, "no allowed values"),
        ({"variables": {"role": ["a", "a"], "colour": ["x"], "pose": ["y"]}}, "twice"),
        ({"variables": {"role": ["{x}"], "colour": ["x"], "pose": ["y"]}}, "braces"),
        ({"id": "Bad ID"}, "id"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_invalid_templates(template_doc, change, match):
    with pytest.raises(ValueError, match=match):
        Template.model_validate({**template_doc, **change})


def test_draft_needs_no_approval(template_doc):
    Template.model_validate({**template_doc, "status": "draft", "approved_by": None, "approved_at": None})


def test_literal_braces_allowed(template_doc):
    t = Template.model_validate({**template_doc, "prompt": "{{logo}} {role} {colour} {pose}"})
    assert fill(t, VALUES, 1).prompt == "{logo} ranger crimson on guard"
    assert placeholders(t.prompt) == ["role", "colour", "pose"]


def test_hash_ignores_status_but_not_content(template_doc):
    approved = Template.model_validate(template_doc)
    retired = Template.model_validate({**template_doc, "status": "retired"})
    changed = Template.model_validate({**template_doc, "negative": "text"})
    assert approved.hash() == retired.hash()
    assert approved.hash() != changed.hash()
    assert len(approved.hash()) == 32
    assert fill(approved, VALUES, 1).template_hash == approved.hash()


def test_hash_normalises_defaults(template_doc):
    explicit = Template.model_validate(template_doc)
    implicit = Template.model_validate({k: v for k, v in template_doc.items() if k != "max_attempts"})
    assert explicit.hash() == implicit.hash()


def test_load_registry(tmp_path, template_doc):
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(template_doc))
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.yml").write_text(yaml.safe_dump({**template_doc, "version": 2, "status": "draft"}))
    reg = load_registry(tmp_path)
    assert sorted(reg) == [("test-portrait", 1), ("test-portrait", 2)]
    assert reg[("test-portrait", 2)].status == "draft"


def test_load_registry_rejects_duplicates(tmp_path, template_doc):
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(template_doc))
    (tmp_path / "b.yaml").write_text(yaml.safe_dump(template_doc))
    with pytest.raises(TemplateError, match="duplicate"):
        load_registry(tmp_path)


def test_load_template_errors(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n")
    with pytest.raises(TemplateError, match="mapping"):
        load_template(bad)
    bad.write_text("id: [unclosed\n")
    with pytest.raises(TemplateError, match="YAML"):
        load_template(bad)


def test_repo_example_templates_are_valid_drafts():
    reg = load_registry(REPO_TEMPLATES)
    assert reg
    assert all(t.status == "draft" for t in reg.values())
