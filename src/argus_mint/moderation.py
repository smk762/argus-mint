"""Auto-moderation: the platform gates on safety, the owner gates on quality.

When it is on (``ARGUS_MINT_AUTO_MODERATE=1``), a draft whose every part passed the platform's
checks gets the moderator's approval recorded automatically, ``by`` ``auto: platform checks``.
The owner still reviews in person. Anything a check can't vouch for is *held* for a human
moderator instead, with the reasons in ``draft.auto_check.held``.

The checks:

- **Art:** rendered by darkroom, and its final attempt passed darkroom's image guards outright.
  A supplied or placeholder image, or one darkroom sent to ``review``, is held.
- **Bio:** written by the model and passed darkroom's text check. A bio a person edited
  afterwards reached no model (hard rule 1), so it goes through the word filter instead.
- **Free text:** the name, an edited bio, the description and the consumer's display text are
  never sent to a model (hard rule 1). They pass a word filter: better-profanity's list, which
  catches letter swaps like "5h1t", plus any terms in ``ARGUS_MINT_BLOCKLIST`` (one per line).

SingGuard judges safety, not quality: a missing figure or garbled lettering is the owner's to
catch.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from argus_mint.cards import AutoCheck, CardDraft, Review, sha256_hex

__all__ = ["AUTO_REVIEWER", "WordFilter", "auto_check", "auto_review"]

AUTO_REVIEWER = "auto: platform checks"


class WordFilter:
    """better-profanity's list plus your own terms. Matching ignores case and letter swaps."""

    def __init__(self, extra_terms: list[str] | None = None):
        try:
            from better_profanity import Profanity
        except ImportError as e:  # pragma: no cover - depends on the environment
            raise RuntimeError('the word filter needs better-profanity: pip install "argus-mint[server]"') from e
        self._filter = Profanity()
        self._filter.load_censor_words()
        terms = [t.strip() for t in extra_terms or [] if t.strip() and not t.strip().startswith("#")]
        if terms:
            self._filter.add_censor_words(terms)

    @classmethod
    def from_file(cls, path: Path | None) -> WordFilter:
        terms = Path(path).read_text(encoding="utf-8").splitlines() if path else []
        return cls(terms)

    def flags(self, text: str) -> bool:
        return bool(text) and self._filter.contains_profanity(re.sub(r"\s+", " ", text))


def auto_check(draft: CardDraft, words: WordFilter) -> AutoCheck:
    """Check the current image's draft; ``passed`` means nothing needs a human moderator."""
    checks: list[str] = []
    held: list[str] = []

    if draft.renderer != "darkroom":
        held.append(f"the art was {draft.renderer}, not rendered and checked by darkroom")
    else:
        last = draft.attempts[-1] if draft.attempts else {}
        if last.get("outcome") == "ok":
            checks.append("art: darkroom's image guards passed it")
        else:
            held.append("darkroom did not pass the art outright")

    free: dict[str, str] = {"name": draft.name}
    if draft.bio is not None:
        if draft.bio.edited:
            free["bio"] = draft.bio.text
        elif draft.bio.attempts and draft.bio.attempts[-1].get("outcome") == "ok":
            checks.append("bio: darkroom's text check passed it")
        else:
            held.append("the bio has no passing text check")
    if draft.description and (draft.bio is None or draft.description != draft.bio.text):
        free["description"] = draft.description
    for slot, text in sorted(draft.texts.items()):
        free[f"text {slot}"] = text

    flagged = [field for field, text in free.items() if words.flags(text)]
    if flagged:
        held.append(f"the word filter flagged: {', '.join(flagged)}")
    else:
        checks.append(f"word filter: {', '.join(free)} clean")

    return AutoCheck(
        at=datetime.now(UTC),
        image_sha256=draft.image_sha256,
        passed=not held,
        checks=tuple(checks),
        held=tuple(held),
    )


def auto_review(draft: CardDraft, image: bytes, words: WordFilter) -> CardDraft:
    """Run :func:`auto_check`; record it, and the moderator's approval if it passed.

    Does nothing to the reviews if the moderator already decided on this image.
    """
    if sha256_hex(image) != draft.image_sha256:
        raise ValueError("the image doesn't match the draft")
    result = auto_check(draft, words)
    draft = draft.model_copy(update={"auto_check": result})
    if not result.passed or "moderator" not in draft.awaiting() or draft.status != "pending":
        return draft
    approval = Review(
        role="moderator",
        decision="approved",
        by=AUTO_REVIEWER,
        at=result.at,
        image_sha256=draft.image_sha256,
        note="; ".join(result.checks),
    )
    return draft.model_copy(update={"reviews": (*draft.reviews, approval)})
