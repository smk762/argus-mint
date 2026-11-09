"""Card drafts and image review: the human approval step between rendering and publishing.

A rendered card is first a *draft*: the unframed art, the finished card image (framed, or
the art itself) and ``draft.json``, kept in a local directory. Nothing is pinned or signed
yet. Before it can be published, the moderator **and** the card's owner each review the
image. Each decision is bound to the image's sha256, so a changed file voids it. A
rejection is final for that draft: render a new draft rather than re-reviewing this one.

:func:`check_publishable` is the gate ``argus-mint publish`` runs before pinning or signing.
Reviews stay in the draft file; they are not written into the public metadata.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from argus_mint._codec import Bytes32
from argus_mint.metadata import FrameRef, Provenance
from argus_mint.templates import RenderRequest

__all__ = [
    "REVIEWERS",
    "CardDraft",
    "DraftError",
    "NotApproved",
    "Review",
    "check_publishable",
    "load_draft",
    "review",
    "save_draft",
]

Role = Literal["moderator", "owner"]
REVIEWERS: tuple[Role, ...] = ("moderator", "owner")  # every one of these must approve
DRAFT_FILE = "draft.json"
ART_FILE = "art.png"
CARD_FILE = "card.png"


class DraftError(ValueError):
    """A draft is missing, malformed, or its files don't match what was reviewed."""


class NotApproved(DraftError):
    """The draft lacks an approval, or was rejected."""


class Review(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Role
    decision: Literal["approved", "rejected"]
    by: str = Field(min_length=1, max_length=128)
    at: datetime
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # the image this decision is about
    note: str = Field(default="", max_length=2000)


class CardDraft(BaseModel):
    """Everything needed to publish a card, except the pins and the signature."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal[1] = 1
    request: RenderRequest  # the request that produced the art (its seed is the final seed)
    attempts: tuple[dict[str, Any], ...]
    renderer: str
    asset_id: Bytes32
    to: str
    name: str = Field(min_length=1)
    description: str = ""
    external_url: str | None = None
    consumer: str = Field(min_length=1, max_length=64)
    frame: FrameRef | None = None
    art_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # the finished card image (== art when unframed)
    created_at: datetime
    reviews: tuple[Review, ...] = ()

    @property
    def status(self) -> Literal["pending", "approved", "rejected"]:
        current = [r for r in self.reviews if r.image_sha256 == self.image_sha256]
        if any(r.decision == "rejected" for r in current):
            return "rejected"
        approved = {r.role for r in current if r.decision == "approved"}
        return "approved" if approved >= set(REVIEWERS) else "pending"

    def awaiting(self) -> list[str]:
        """Roles that have not reviewed the current image yet."""
        done = {r.role for r in self.reviews if r.image_sha256 == self.image_sha256}
        return [r for r in REVIEWERS if r not in done]

    def provenance(self, art_cid: str | None) -> Provenance:
        """The card's provenance. ``art_cid`` (the unframed render) is required for a framed card."""
        return Provenance(
            template=self.request.template_id,
            template_version=self.request.template_version,
            template_hash=self.request.template_hash,
            seed=self.request.seed,
            attempts=len(self.attempts),
            asset_id=self.asset_id,
            consumer=self.consumer,
            frame=self.frame,
            art=f"ipfs://{art_cid}" if self.frame is not None and art_cid else None,
        )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def review(
    draft: CardDraft,
    *,
    role: Role,
    approve: bool,
    by: str,
    note: str = "",
    at: datetime | None = None,
) -> CardDraft:
    """``draft`` with one more decision. Each role decides once per image; a rejected draft is closed."""
    if draft.status == "rejected":
        raise DraftError("this draft was rejected; render a new draft instead")
    if role not in draft.awaiting():
        raise DraftError(f"the {role} has already reviewed this image")
    decision = Review(
        role=role,
        decision="approved" if approve else "rejected",
        by=by,
        at=at or datetime.now(UTC),
        image_sha256=draft.image_sha256,
        note=note,
    )
    return draft.model_copy(update={"reviews": (*draft.reviews, decision)})


def check_publishable(draft: CardDraft, art: bytes, image: bytes) -> None:
    """Raise unless both reviewers approved exactly these bytes. Run before pinning or signing."""
    if sha256_hex(art) != draft.art_sha256:
        raise DraftError("the art file differs from the one rendered for this draft")
    if sha256_hex(image) != draft.image_sha256:
        raise DraftError("the card image differs from the one reviewed")
    status = draft.status
    if status == "rejected":
        raise NotApproved("the card image was rejected in review")
    if status != "approved":
        raise NotApproved(f"the card image still needs approval from: {', '.join(draft.awaiting())}")


def save_draft(directory: Path, draft: CardDraft, art: bytes | None = None, image: bytes | None = None) -> None:
    """Write ``draft.json`` (and the images, when given) into ``directory``."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if art is not None:
        (directory / ART_FILE).write_bytes(art)
    if image is not None and draft.frame is not None:
        (directory / CARD_FILE).write_bytes(image)
    (directory / DRAFT_FILE).write_text(draft.model_dump_json(indent=2) + "\n", encoding="utf-8")


def load_draft(directory: Path) -> tuple[CardDraft, bytes, bytes]:
    """The draft in ``directory`` with its art and card image bytes."""
    directory = Path(directory)
    try:
        draft = CardDraft.model_validate(json.loads((directory / DRAFT_FILE).read_text(encoding="utf-8")))
        art = (directory / ART_FILE).read_bytes()
        image = (directory / CARD_FILE).read_bytes() if draft.frame is not None else art
    except OSError as e:
        raise DraftError(f"{directory}: {e}") from e
    except (ValueError, ValidationError) as e:
        raise DraftError(f"{directory}/{DRAFT_FILE}: {e}") from e
    return draft, art, image
