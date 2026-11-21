"""Audit fixes: auto-moderation off holds every draft; identity outcomes fail closed."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("better_profanity")

from argus_mint.cards import CardDraft, IdentityCheck  # noqa: E402
from argus_mint.moderation import AUTO_OFF, WordFilter, auto_check, auto_review  # noqa: E402
from argus_mint.templates import RenderRequest  # noqa: E402

IMAGE = b"card image"


def card_draft() -> CardDraft:
    from argus_mint.cards import sha256_hex

    request = RenderRequest(
        template_id="t",
        template_version=1,
        template_hash="0x" + "00" * 32,
        model="m",
        loras=(),
        sampling=None,
        width=64,
        height=64,
        prompt="p",
        negative="",
        variables={},
        seed=1,
    )
    return CardDraft(
        request=request,
        attempts=({"seed": 1, "outcome": "ok"},),
        renderer="darkroom",
        asset_id="0x" + "11" * 32,
        to="0x" + "22" * 20,
        name="Clean Name",
        consumer="test",
        art_sha256=sha256_hex(IMAGE),
        image_sha256=sha256_hex(IMAGE),
        created_at=datetime.now(UTC),
    )


def test_auto_moderation_off_holds_a_card_too():
    words = WordFilter()
    draft = card_draft()
    assert auto_check(draft, words).passed  # clean art and text: on, it stands in for the moderator
    off = auto_check(draft, words, approve=False)
    assert not off.passed and off.held == (AUTO_OFF,)
    reviewed = auto_review(draft, IMAGE, words, approve=False)
    assert reviewed.auto_check is not None and not reviewed.auto_check.passed
    assert "moderator" in reviewed.awaiting()


@pytest.mark.parametrize(
    ("outcome", "presence", "held"),
    [
        ("passed", "embodied", False),
        ("uncalibrated", "embodied", False),  # only recorded; auto_check still won't let it approve
        ("skipped", "viewpoint", False),
        ("skipped", "embodied", True),
        ("below", "embodied", True),
        ("unavailable", "embodied", True),
        ("error", "embodied", True),
    ],
)
def test_identity_held_is_an_allow_list(outcome, presence, held):
    c = IdentityCheck.model_construct(outcome=outcome, presence=presence, reason="r")
    assert c.held is held
    # An outcome nobody listed (added later) holds by default.
    assert IdentityCheck.model_construct(outcome="new-outcome", presence="embodied", reason="r").held


def test_spoiler_rules_match_each_text_on_its_own():
    from argus_mint.spoilers import parse

    rules = parse("re:^Spoiler:\ndragon king\n")
    assert rules.hits("a title", "Spoiler: the end") == [1]  # anchors hold for every text, not only the first
    assert rules.hits("the dragon", "king of nothing") == []  # no match across two texts
    assert rules.hits("the dragon\nking", "x") == [2]  # within one text a phrase still spans lines
