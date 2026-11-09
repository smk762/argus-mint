from __future__ import annotations

import pytest

from argus_mint.render import GenResult, Outcome, RenderError, RenderFailed, derive_seed, render
from argus_mint.templates import NotApprovedError, RenderRequest, Template

VALUES = {"role": "ranger", "colour": "crimson", "pose": "on guard"}


class ScriptedDarklight:
    """Returns a scripted outcome per call and records every request it saw."""

    def __init__(self, *outcomes: GenResult):
        self.outcomes = list(outcomes)
        self.requests: list[RenderRequest] = []

    def generate(self, req: RenderRequest) -> GenResult:
        self.requests.append(req)
        return self.outcomes.pop(0)


def test_first_attempt_ok(template_doc):
    t = Template.model_validate(template_doc)
    dl = ScriptedDarklight(GenResult.ok(b"png"))
    r = render(dl, t, VALUES, seed=81234)
    assert r.image == b"png" and r.seed == 81234
    assert [a.outcome for a in r.attempts] == [Outcome.OK]


def test_retries_safety_rejections_with_new_seeds(template_doc):
    t = Template.model_validate(template_doc)
    dl = ScriptedDarklight(GenResult.safety_rejected("nsfw"), GenResult.safety_rejected("nsfw"), GenResult.ok(b"png"))
    r = render(dl, t, VALUES, seed=5)
    seeds = [req.seed for req in dl.requests]
    assert seeds == [5, derive_seed(5, 1), derive_seed(5, 2)]
    assert len(set(seeds)) == 3
    assert [a.outcome for a in r.attempts] == [Outcome.SAFETY_REJECTED, Outcome.SAFETY_REJECTED, Outcome.OK]
    assert [a.seed for a in r.attempts] == seeds
    assert r.seed == seeds[-1]
    # retry, don't loosen: the prompt never changes between attempts
    assert {req.prompt for req in dl.requests} == {dl.requests[0].prompt}
    assert {req.negative for req in dl.requests} == {t.negative}


def test_stops_at_max_attempts(template_doc):
    t = Template.model_validate({**template_doc, "max_attempts": 3})
    dl = ScriptedDarklight(*[GenResult.safety_rejected()] * 5)
    with pytest.raises(RenderFailed) as err:
        render(dl, t, VALUES, seed=1)
    assert len(dl.requests) == 3
    assert len(err.value.attempts) == 3
    assert all(a.outcome is Outcome.SAFETY_REJECTED for a in err.value.attempts)


def test_error_is_not_retried(template_doc):
    t = Template.model_validate(template_doc)
    dl = ScriptedDarklight(GenResult.safety_rejected(), GenResult.error("timeout"), GenResult.ok(b"x"))
    with pytest.raises(RenderError, match="timeout") as err:
        render(dl, t, VALUES, seed=1)
    assert not isinstance(err.value, RenderFailed)
    assert [a.outcome for a in err.value.attempts] == [Outcome.SAFETY_REJECTED, Outcome.ERROR]


def test_empty_image_is_an_error(template_doc):
    with pytest.raises(RenderError, match="no image"):
        render(ScriptedDarklight(GenResult.ok(b"")), Template.model_validate(template_doc), VALUES, seed=1)


def test_unapproved_template_never_reaches_darklight(template_doc):
    dl = ScriptedDarklight(GenResult.ok(b"png"))
    with pytest.raises(NotApprovedError):
        render(dl, Template.model_validate({**template_doc, "status": "retired"}), VALUES, seed=1)
    assert dl.requests == []


def test_derive_seed_is_deterministic_and_in_range():
    assert derive_seed(42, 0) == 42
    assert derive_seed(42, 1) == derive_seed(42, 1)
    assert derive_seed(42, 1) != derive_seed(42, 2) != derive_seed(43, 1)
    assert all(0 <= derive_seed(s, n) < 2**32 for s in (0, 2**32 - 1) for n in range(1, 10))


def test_provenance_from_render(template_doc):
    t = Template.model_validate(template_doc)
    r = render(ScriptedDarklight(GenResult.safety_rejected(), GenResult.ok(b"png")), t, VALUES, seed=9)
    p = r.provenance(asset_id=b"\x01" * 32, consumer="atlas-realms")
    assert (p.template, p.template_version, p.template_hash) == (t.id, t.version, t.hash())
    assert (p.seed, p.attempts) == (derive_seed(9, 1), 2)


def test_render_for_review_renders_drafts_but_render_does_not(template_doc):
    from argus_mint.render import render_for_review
    from argus_mint.templates import NotApprovedError, Template

    t = Template.model_validate({**template_doc, "status": "draft", "approved_by": None, "approved_at": None})
    values = {"role": "ranger", "colour": "none", "pose": "standing"}

    class Ok:
        def generate(self, req):
            return GenResult.ok(b"img")

    assert render_for_review(Ok(), t, values, 7).image == b"img"
    with pytest.raises(NotApprovedError):
        render(Ok(), t, values, 7)
