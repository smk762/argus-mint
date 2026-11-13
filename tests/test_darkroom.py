"""DarkroomClient against a fake darkroom (httpx MockTransport speaking its SSE API)."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from argus_mint.darkroom import DarkroomClient, DarkroomError, check_template, final_prompt, request_body
from argus_mint.render import Outcome, RenderFailed, render
from argus_mint.templates import Template, fill

VALUES = {"role": "ranger", "colour": "crimson", "pose": "on guard"}
PNG = b"\x89PNG fake image bytes"


@pytest.fixture
def tdoc(template_doc):
    return {
        **template_doc,
        "loras": [{"name": "fantasy-portrait.safetensors", "weight": 0.8, "trigger_words": ["fp style"]}],
        "sampling": {"sampler": "euler", "scheduler": "normal", "steps": 20, "cfg": 5.0},
    }


class FakeDarkroom:
    """Answers /generate/stream with a scripted outcome per call, and serves /image/{id}."""

    def __init__(self, *outcomes: str, busy: int = 0, tamper: str | None = None):
        self.outcomes = list(outcomes)
        self.busy = busy
        self.tamper = tamper
        self.bodies: list[dict] = []
        self.images: dict[str, bytes] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/generate/stream":
            if self.busy:
                self.busy -= 1
                return httpx.Response(409, json={"detail": "a generation is already running"})
            body = json.loads(request.content)
            self.bodies.append(body)
            return self._stream(body, self.outcomes.pop(0))
        if path.startswith("/image/"):
            rid = path.rsplit("/", 1)[1]
            return httpx.Response(200, content=self.images[rid]) if rid in self.images else httpx.Response(404)
        if path == "/preflight":
            return httpx.Response(
                200, json={"outcome": "ok", "final_prompt": json.loads(request.content)["user_prompt"]}
            )
        return httpx.Response(404)

    def _stream(self, body: dict, outcome: str) -> httpx.Response:
        rid = f"rec{len(self.bodies)}"
        triggers = [w for lo in body["loras"] for w in lo["trigger_words"]]
        final = ", ".join([body["user_prompt"], *triggers])
        record = {
            "id": rid,
            "outcome": outcome,
            "outcome_reason": {"deleted": "default:A", "omitted": "sfw-exposed", "review": "default:S1"}.get(outcome),
            "request": {
                "final_prompt": final,
                "expanded_prompt": None,
                "seed": body["seed"],
                "workflow": f"sdxl+lora{len(body['loras'])}" if body["loras"] else "sdxl",
                "workflow_sha256": "ab" * 32,
            },  # fmt: skip
            "guard": {"enforcing_policy": body["enforcing_policy"]},
            "image": None,
        }
        if outcome in ("passed", "review"):
            self.images[rid] = PNG
            record["image"] = {"sha256": hashlib.sha256(PNG).hexdigest()}
        if self.tamper == "seed":
            record["request"]["seed"] = body["seed"] + 1
        if self.tamper == "image":
            self.images[rid] = b"something else"
        events = [("preflight", {"outcome": "ok"}), ("progress", {"step": 1, "total": 20})]
        events.append(("error", {"detail": "comfyui down"}) if outcome == "error" else ("complete", {"record": record}))
        text = "".join(f"event: {k}\ndata: {json.dumps(v)}\n\n" for k, v in events)
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})


def client(fake: FakeDarkroom, **kw) -> DarkroomClient:
    return DarkroomClient("http://darkroom:8106", client=httpx.Client(transport=httpx.MockTransport(fake)), **kw)


def test_request_body(tdoc):
    req = fill(Template.model_validate(tdoc), VALUES, 7)
    body = request_body(req)
    assert body["expand"] is False
    assert body["enforcing_policy"] == "default"
    assert body["seed"] == 7 and body["checkpoint"] == "juggernaut-xl"
    assert body["sampling"] == {
        "sampler": "euler", "scheduler": "normal", "steps": 20, "cfg": 5.0, "clip_skip": 1, "width": 1024, "height": 1024
    }  # fmt: skip
    assert body["loras"] == [{"name": "fantasy-portrait.safetensors", "weight": 0.8, "trigger_words": ["fp style"]}]
    assert final_prompt(req) == req.prompt + ", fp style"


def test_template_without_sampling_is_refused(template_doc):
    req = fill(Template.model_validate(template_doc), VALUES, 7)
    with pytest.raises(DarkroomError, match="sampling"):
        request_body(req)


def test_passed_returns_verified_image(tdoc):
    fake = FakeDarkroom("passed")
    r = render(client(fake), Template.model_validate(tdoc), VALUES, seed=5)
    assert r.image == PNG
    assert r.attempts[0].ref == "rec1"
    # darkroom's workflow is kept with the attempt, so a draft and its publish record name it
    assert r.attempts[0].workflow == {"name": "sdxl+lora1", "sha256": "ab" * 32}
    assert r.attempts[0].to_json()["workflow"] == {"name": "sdxl+lora1", "sha256": "ab" * 32}


def _models(*checkpoints, workflows=(), loras=("fantasy-portrait.safetensors",)):
    return {
        "checkpoints": [{"name": n, "family": n.split("/")[0], "lora_counts": counts} for n, counts in checkpoints],
        "loras": list(loras),
        "workflows": list(workflows),
        "samplers": ["euler"],
        "schedulers": ["normal", "simple"],
    }


def _wf(name, family, loras, *slots):
    return {"name": name, "family": family, "loras": loras, "slots": ["$positive", "$seed", *slots]}


def test_check_template_against_darkroom_models(tdoc):
    sdxl, flux = "sdxl/a/a.safetensors", "flux/f/f.safetensors"
    flux_sampling = {"sampler": "euler", "scheduler": "simple", "steps": 20, "cfg": 1.0, "guidance": 3.5}
    t = Template.model_validate(
        {**tdoc, "model": sdxl, "models": [flux], "negative": "blurry", "negative_by_family": {"flux": ""},
         "sampling_by_family": {"flux": flux_sampling}}
    )  # fmt: skip
    workflows = [
        _wf("sdxl+lora1", "sdxl", 1, "$negative", "$clip_skip"),
        _wf("flux+lora1", "flux", 1, "$guidance"),
    ]
    good = _models((sdxl, [0, 1]), (flux, [0, 1]), workflows=workflows)
    assert check_template(t, good) == []
    # Flux would get the negative prompt and no guidance; a LoRA darkroom lacks; a checkpoint it lacks
    bad = Template.model_validate({**t.model_dump(mode="json"), "negative_by_family": {}, "sampling_by_family": {
        "flux": {**flux_sampling, "guidance": None}}, "models": [flux, "sdxl/gone/gone.safetensors"]})  # fmt: skip
    problems = check_template(bad, _models((sdxl, [0, 1]), (flux, [0, 1]), workflows=workflows, loras=["other"]))
    assert any("takes no negative prompt" in p and "flux" in p for p in problems)
    assert any("needs guidance" in p for p in problems)
    assert any("LoRA fantasy-portrait.safetensors" in p for p in problems)
    assert any("sdxl/gone/gone.safetensors: darkroom has no such checkpoint" in p for p in problems)
    # a checkpoint without a workflow for the template's LoRA count
    assert any(
        "with [1] LoRA(s)" in p for p in check_template(t, _models((sdxl, [0]), (flux, [0, 1]), workflows=workflows))
    )
    # darkroom with no LoRA files at all can't render the template's LoRA
    no_loras = check_template(t, _models((sdxl, [0, 1]), (flux, [0, 1]), workflows=workflows, loras=[]))
    assert any("LoRA fantasy-portrait.safetensors" in p for p in no_loras)
    # a family-agnostic workflow listed first doesn't hide the family's own (which lacks $guidance)
    generic_first = [_wf("any+lora1", None, 1, "$guidance", "$negative", "$clip_skip"), *workflows]
    generic_first[2] = _wf("flux+lora1", "flux", 1)
    assert any(
        "flux+lora1" in p and "takes no guidance" in p
        for p in check_template(t, _models((sdxl, [0, 1]), (flux, [0, 1]), workflows=generic_first))
    )


def test_safety_outcomes_retry_with_new_seeds(tdoc):
    fake = FakeDarkroom("deleted", "omitted", "review", "passed")
    r = render(client(fake), Template.model_validate(tdoc), VALUES, seed=5)
    assert [a.outcome for a in r.attempts][:3] == [Outcome.SAFETY_REJECTED] * 3
    assert len({b["seed"] for b in fake.bodies}) == 4
    assert {b["user_prompt"] for b in fake.bodies} == {fake.bodies[0]["user_prompt"]}  # never loosened
    assert r.attempts[0].detail == "deleted: default:A"


def test_review_accepted_when_configured(tdoc):
    r = render(client(FakeDarkroom("review"), accept_review=True), Template.model_validate(tdoc), VALUES, seed=5)
    assert r.image == PNG


def test_all_rejected_raises(tdoc):
    t = Template.model_validate({**tdoc, "max_attempts": 2})
    with pytest.raises(RenderFailed):
        render(client(FakeDarkroom("deleted", "deleted")), t, VALUES, seed=1)


@pytest.mark.parametrize(
    ("outcome", "match"), [("refused", "template needs review"), ("failed", "failed"), ("error", "comfyui down")]
)
def test_non_safety_outcomes_are_errors(tdoc, outcome, match):
    res = client(FakeDarkroom(outcome)).generate(fill(Template.model_validate(tdoc), VALUES, 1))
    assert res.outcome is Outcome.ERROR
    assert match in res.detail


@pytest.mark.parametrize(("tamper", "match"), [("seed", "seed"), ("image", "sha256")])
def test_mismatched_record_is_an_error(tdoc, tamper, match):
    res = client(FakeDarkroom("passed", tamper=tamper)).generate(fill(Template.model_validate(tdoc), VALUES, 1))
    assert res.outcome is Outcome.ERROR
    assert match in res.detail


def test_waits_while_busy(tdoc):
    fake = FakeDarkroom("passed", busy=2)
    res = client(fake, busy_wait=0).generate(fill(Template.model_validate(tdoc), VALUES, 1))
    assert res.outcome is Outcome.OK


def test_gives_up_when_busy_too_long(tdoc):
    fake = FakeDarkroom("passed", busy=100)
    c = client(fake, busy_wait=0, busy_timeout=0)
    with pytest.raises(httpx.HTTPStatusError):
        c._stream(fill(Template.model_validate(tdoc), VALUES, 1))
    assert (
        client(FakeDarkroom(busy=100), busy_wait=0, busy_timeout=0)
        .generate(fill(Template.model_validate(tdoc), VALUES, 1))
        .outcome
        is Outcome.ERROR
    )


def test_unreachable_is_an_error(tdoc):
    def boom(request):
        raise httpx.ConnectError("refused")

    c = DarkroomClient(client=httpx.Client(transport=httpx.MockTransport(boom)))
    assert c.generate(fill(Template.model_validate(tdoc), VALUES, 1)).outcome is Outcome.ERROR


def test_preflight(tdoc):
    out = client(FakeDarkroom()).preflight(fill(Template.model_validate(tdoc), VALUES, 1))
    assert out["outcome"] == "ok"
