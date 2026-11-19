"""moderation.WordFilter: custom blocklist terms match whatever their case."""

from __future__ import annotations

import pytest

pytest.importorskip("better_profanity")

from argus_mint.moderation import WordFilter  # noqa: E402


def test_custom_terms_match_in_any_case():
    words = WordFilter(["ACME", "BadTerm", "# a comment", "  "])
    for text in ("acme", "ACME", "Acme rocks", "a badterm here", "BADTERM"):
        assert words.flags(text), text
    assert not words.flags("a perfectly fine name")
