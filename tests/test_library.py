"""The pre-rendered library: building it, checking entries against templates, and drafts from it."""

from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from argus_mint.library import Library, LibraryError, build, check_entry, model_slug, pick_variable_sets
from argus_mint.render import GenResult
from argus_mint.templates import Template, TemplateError, fill

from .test_frames import ART

MODELS = ["sdxl/a/a.safetensors", "sdxl/b/b.safetensors"]


@pytest.fixture
def tdoc(template_doc):
    return {**template_doc, "loras": [], "model": MODELS[0], "models": MODELS}


class Renderer:
    def __init__(self, reject_first=False):
        self.calls = []
        self.reject_first = reject_first

    def generate(self, req):
        self.calls.append(req)
        if self.reject_first and len(self.calls) == 1:
            return GenResult.safety_rejected("deleted", ref="r0")
        return GenResult.ok(ART, ref=f"r{len(self.calls)}")


def test_models_are_a_reviewed_choice_and_old_hashes_hold(template_doc, tdoc):
    plain = Template.model_validate(template_doc)
    assert plain.allowed_models() == (plain.model,)
    t = Template.model_validate(tdoc)
    assert fill(t, {"role": "ranger", "colour": "none", "pose": "standing"}, 1, MODELS[1]).model == MODELS[1]
    with pytest.raises(TemplateError, match="not one this template allows"):
        fill(t, {"role": "ranger", "colour": "none", "pose": "standing"}, 1, "sdxl/c/c.safetensors")
    assert t.hash() != plain.hash()  # the list is reviewed: it's in the hash
    assert Template.model_validate({**template_doc, "models": []}).hash() == plain.hash()  # absent = before


def test_variable_sets_cover_every_value(tdoc):
    t = Template.model_validate(tdoc)
    sets = pick_variable_sets(t, 8, 7)
    assert len({tuple(sorted(s.items())) for s in sets}) == 8
    for name, values in t.variables.items():
        assert {s[name] for s in sets} == set(values)
    assert pick_variable_sets(t, 8, 7) == sets  # deterministic


def test_build_is_model_major_resumable_and_compares_like_for_like(tmp_path, tdoc):
    t = Template.model_validate({**tdoc, "status": "draft", "approved_by": None, "approved_at": None})
    r = Renderer(reject_first=True)
    lib = build(r, t, tmp_path / "lib", count=3, seed=5)
    assert len(lib.entries) == 6 and [c.model for c in r.calls[:4]] == [MODELS[0]] * 4  # one model at a time
    by_group = {}
    for e in lib.entries:
        by_group.setdefault(e.group, []).append(e)
    for entries in by_group.values():
        assert len({json.dumps(e.variables, sort_keys=True) for e in entries}) == 1  # same variables
        assert len({e.seed for e in entries}) == 1  # and the same seed, on every checkpoint
    assert lib.entries[0].attempts[0]["outcome"] == "safety_rejected" and len(lib.entries[0].attempts) == 2
    calls = len(r.calls)
    build(r, t, tmp_path / "lib", count=3, seed=5)
    assert len(r.calls) == calls  # resumed: nothing re-rendered
    with pytest.raises(LibraryError, match="other settings"):
        build(r, t, tmp_path / "lib", count=4, seed=5)
    assert model_slug(MODELS[1]) == "b" and lib.image(lib.entries[0].id) == ART
    assert lib.thumbnail(lib.entries[0].id, 64)[:2] == b"\xff\xd8"  # a JPEG


class Failing:
    def __init__(self, result):
        self.result = result

    def generate(self, req):
        return self.result


def test_build_refuses_what_it_cannot_do(tmp_path, tdoc):
    t = Template.model_validate(tdoc)
    with pytest.raises(LibraryError, match="only 8 combinations"):
        pick_variable_sets(t, 9, 0)  # more than there are: no ninth one to find
    clash = Template.model_validate({**tdoc, "models": [MODELS[0], "sd15/x/A.safetensors"]})
    with pytest.raises(LibraryError, match="share entry ids"):
        build(Renderer(), clash, tmp_path / "clash", count=2, seed=1)


def test_a_resumed_build_retries_errors_and_missing_images_but_not_rejections(tmp_path, tdoc):
    t = Template.model_validate(tdoc)
    down = build(Failing(GenResult.error("darkroom unreachable")), t, tmp_path / "lib", count=2, seed=1)
    assert len(down.manifest.failed) == 4 and not any(f["rejected"] for f in down.manifest.failed)
    lib = build(Renderer(), t, tmp_path / "lib", count=2, seed=1)  # darkroom is back: the errors are tried again
    assert len(lib.entries) == 4 and lib.manifest.failed == []

    (tmp_path / "lib" / lib.entries[0].file).unlink()
    with pytest.raises(LibraryError, match="can't be read"):
        lib.image(lib.entries[0].id)
    lib = build(Renderer(), t, tmp_path / "lib", count=2, seed=1)
    assert len(lib.entries) == 4 and len({e.id for e in lib.entries}) == 4  # rendered again, listed once
    assert lib.image(lib.entries[-1].id) == ART

    build(Failing(GenResult.safety_rejected("deleted")), t, tmp_path / "no", count=2, seed=1)
    r = Renderer()
    refused = build(r, t, tmp_path / "no", count=2, seed=1)
    assert r.calls == [] and len(refused.manifest.failed) == 4  # a safety rejection is final


def test_entries_must_be_what_the_approved_template_renders(tmp_path, tdoc):
    draft_t = Template.model_validate({**tdoc, "status": "draft", "approved_by": None, "approved_at": None})
    lib = build(Renderer(), draft_t, tmp_path / "lib", count=2, seed=1)
    approved = Template.model_validate(tdoc)  # approval changes the hash, not what renders
    req = check_entry(lib.entries[0], approved)
    assert req.model == lib.entries[0].model and req.template_hash == approved.hash()
    changed = Template.model_validate({**tdoc, "negative": "something else"})
    with pytest.raises(LibraryError, match="different request"):
        check_entry(lib.entries[0], changed)
    with pytest.raises(LibraryError, match="only approved"):
        check_entry(lib.entries[0], draft_t)  # drafts can't make cards


def test_server_drafts_from_the_library_without_darkroom(tmp_path, tdoc):
    from argus_mint.server import Settings, create_app

    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "t.yaml").write_text(yaml.safe_dump(tdoc))
    draft_t = Template.model_validate({**tdoc, "status": "draft", "approved_by": None, "approved_at": None})
    lib = build(Renderer(), draft_t, tmp_path / "library", count=2, seed=1)
    cfg = Settings(
        drafts_dir=tmp_path / "drafts", samples_dir=tmp_path / "samples", templates_dir=tmp_path / "templates",
        frames_dir=tmp_path / "frames", bios_dir=tmp_path / "bios", library_dir=tmp_path / "library",
        moderator_token="t",
    )  # fmt: skip
    c = TestClient(create_app(cfg, probe_fn=lambda u: False))
    caps = c.get("/health").json()["capabilities"]
    assert caps["library"] is True and caps["create_drafts"] is True  # no darkroom needed
    listing = c.get("/library").json()
    assert len(listing["items"]) == 4 and listing["models"] == MODELS
    entry = listing["items"][1]
    assert c.get(f"/library/{entry['id']}/image?thumb=96").headers["content-type"] == "image/jpeg"
    body = {"template": {"id": tdoc["id"], "version": 1}, "library": entry["id"], "asset": "a1",
            "to": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8", "name": "Hana"}  # fmt: skip
    r = c.post("/drafts", json=body, headers={"Authorization": "Bearer t"})
    assert r.status_code == 201, r.text
    d = r.json()["draft"]
    assert d["library"] == entry["id"] and d["request"]["model"] == entry["model"] and d["renderer"] == "darkroom"
    assert d["attempts"] == list(lib.entry(entry["id"]).attempts)
    bad = c.post("/drafts", json=body | {"library": "999-nope"}, headers={"Authorization": "Bearer t"})
    assert bad.status_code == 404
    render = c.post(
        "/drafts", json=body | {"library": None, "variables": entry["variables"]}, headers={"Authorization": "Bearer t"}
    )
    assert render.status_code == 503  # rendering needs darkroom
    assert Library(tmp_path / "library").entry(entry["id"]).model == entry["model"]


def test_lora_options_are_a_reviewed_choice(tdoc):
    from argus_mint.templates import LoraChoice

    doc = {
        **tdoc,
        "lora_options": [
            {"name": "sdxl/style/ink.safetensors", "weights": [0.6, 0.9], "trigger_words": ["ink style"],
             "models": [MODELS[0]]},
            {"name": "sdxl/style/glow.safetensors"},
        ],
        "max_loras": 1,
    }  # fmt: skip
    t = Template.model_validate(doc)
    values = {"role": "ranger", "colour": "none", "pose": "standing"}
    req = fill(t, values, 1, MODELS[0], (LoraChoice(name="sdxl/style/ink.safetensors"),))
    assert [(lo.name, lo.weight, lo.trigger_words) for lo in req.loras] == [
        ("sdxl/style/ink.safetensors", 0.6, ("ink style",))
    ]
    for bad, match in [
        ((LoraChoice(name="sdxl/other.safetensors"),), "not one this template offers"),
        ((LoraChoice(name="sdxl/style/ink.safetensors", weight=2.0),), "allows weights"),
        ((LoraChoice(name="sdxl/style/ink.safetensors"), LoraChoice(name="sdxl/style/glow.safetensors")), "at most 1"),
    ]:
        with pytest.raises(TemplateError, match=match):
            fill(t, values, 1, MODELS[0], bad)
    with pytest.raises(TemplateError, match="doesn't pair"):
        fill(t, values, 1, MODELS[1], (LoraChoice(name="sdxl/style/ink.safetensors"),))
    assert fill(t, values, 1).loras == ()  # choosing none is fine
    with pytest.raises(ValueError, match="doesn't allow"):
        Template.model_validate({**doc, "lora_options": [{"name": "x", "models": ["flux/f/f.safetensors"]}]})


def test_sampling_follows_the_checkpoint_family(template_doc):
    from argus_mint.darkroom import request_body
    from argus_mint.templates import family

    base = {"sampler": "euler", "scheduler": "normal", "steps": 25, "cfg": 6.0}
    flux = {"sampler": "euler", "scheduler": "simple", "steps": 20, "cfg": 1.0, "guidance": 3.5}
    doc = {**template_doc, "loras": [], "model": "sdxl/a/a.safetensors", "models": ["flux/f/f.safetensors"],
           "sampling": base, "sampling_by_family": {"flux": flux}}  # fmt: skip
    t = Template.model_validate(doc)
    values = {"role": "ranger", "colour": "none", "pose": "standing"}
    assert family("flux/f/f.safetensors") == "flux" and family("plain.safetensors") == ""
    sdxl_req = fill(t, values, 1)
    flux_req = fill(t, values, 1, "flux/f/f.safetensors")
    assert sdxl_req.sampling.cfg == 6.0 and sdxl_req.sampling.guidance is None
    assert flux_req.sampling.guidance == 3.5 and flux_req.sampling.steps == 20
    assert "guidance" not in request_body(sdxl_req)["sampling"]  # unset: darkroom never sees it
    assert request_body(flux_req)["sampling"]["guidance"] == 3.5
    plain = Template.model_validate({**template_doc, "sampling": base})
    assert plain.hash() == Template.model_validate({**template_doc, "sampling": {**base, "guidance": None}}).hash()


def test_negative_follows_the_checkpoint_family(template_doc):
    from argus_mint.darkroom import request_body
    from argus_mint.templates import family

    doc = {**template_doc, "loras": [], "model": "sdxl/a/a.safetensors", "models": ["flux/f/f.safetensors"],
        "sampling": {"sampler": "euler", "scheduler": "normal", "steps": 25, "cfg": 6.0},
           "negative": "blurry", "negative_by_family": {"flux": ""}}  # fmt: skip
    t = Template.model_validate(doc)
    values = {"role": "ranger", "colour": "none", "pose": "standing"}
    assert fill(t, values, 1).negative == "blurry"
    flux_req = fill(t, values, 1, "flux/f/f.safetensors")
    assert flux_req.negative == "" and request_body(flux_req)["negative_prompt"] == ""  # Flux has no $negative
    assert family("sdxl/../flux/f.safetensors") == "flux"  # normalised, as darkroom reads it
    unused = {k: v for k, v in doc.items() if k != "negative_by_family"}
    assert (
        Template.model_validate({**unused, "negative_by_family": {}}).hash() == Template.model_validate(unused).hash()
    )
    with pytest.raises(ValueError, match="placeholders"):
        Template.model_validate({**doc, "negative_by_family": {"flux": "no {role}"}})
