"""The HTTP wire contract of ``argus-mint serve``: request and response models.

argus-studio and argus-assay generate their TypeScript types from the committed schema
(``schema/mint-wire.schema.json``, written by ``argus-mint schema``), pinned by release tag
and sha256. Bump :data:`WIRE_VERSION`'s major on any breaking change; a client refuses a
major it doesn't know.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from argus_mint.bios import BioTemplate
from argus_mint.cards import CardDraft
from argus_mint.frames import Frame
from argus_mint.templates import Template

__all__ = [
    "WIRE_VERSION",
    "BioInfo",
    "Capabilities",
    "Challenge",
    "ChallengeRequest",
    "CreateDraft",
    "DraftDetail",
    "DraftList",
    "DraftSummary",
    "FrameInfo",
    "Health",
    "PublishRequest",
    "PublishResult",
    "Ref",
    "ReviewRequest",
    "TemplateInfo",
    "WIRE_MODELS",
]

WIRE_VERSION = "1.0"
DraftStatus = Literal["pending", "approved", "rejected", "published"]


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Capabilities(_Wire):
    """What this server will do. A client disables a control whose capability is false."""

    create_drafts: bool  # darkroom configured, moderator token set, not read-only
    write_bios: bool  # a bio writer configured too
    moderate: bool  # moderator token set, not read-only
    owner_review: bool  # SIWE domain set, not read-only
    publish: bool  # IPFS, signer and Card address configured, moderator token set, not read-only


class Health(_Wire):
    service: Literal["argus-mint"] = "argus-mint"
    version: str
    wire_version: str = WIRE_VERSION
    read_only: bool
    network: str
    chain_id: int
    card: str | None
    consumer: str
    reviewers: list[str]
    siwe_domain: str | None
    capabilities: Capabilities
    backends: dict[str, bool]  # darkroom, writer, ipfs: does something answer there


class TemplateInfo(_Wire):
    hash: str
    template: Template


class FrameInfo(_Wire):
    hash: str
    width: int
    height: int
    frame: Frame


class BioInfo(_Wire):
    hash: str
    bio: BioTemplate


class Ref(_Wire):
    id: str
    version: int = Field(ge=1)


class CreateDraft(_Wire):
    """POST /drafts. The template, frame and bio are named by id and version, never by path."""

    template: Ref
    variables: dict[str, str]
    seed: int = Field(ge=0, le=2**32 - 1)
    asset: str | None = Field(default=None, min_length=1, max_length=256)  # asset id = keccak256(utf8)
    asset_id: str | None = None  # 0x + 64 hex
    to: str
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    external_url: str | None = None
    frame: Ref | None = None
    bio: Ref | None = None
    texts: dict[str, str] = Field(default_factory=dict)  # consumer display text by frame slot


class DraftSummary(_Wire):
    id: str
    name: str
    to: str
    status: DraftStatus
    awaiting: list[str]
    created_at: datetime
    template: Ref
    frame: Ref | None
    image_sha256: str


class DraftList(_Wire):
    items: list[DraftSummary]
    total: int


class PublishResult(_Wire):
    signer: str
    render: dict[str, Any]
    reviews: list[dict[str, Any]]
    metadata: dict[str, Any]
    metadata_cid: str
    voucher: dict[str, Any]
    signature: str
    typed_data: dict[str, Any]


class DraftDetail(_Wire):
    id: str
    status: DraftStatus
    awaiting: list[str]
    draft: CardDraft
    published: PublishResult | None = None


class ChallengeRequest(_Wire):
    """POST /drafts/{id}/challenge: the owner asks for the message to sign."""

    approve: bool


class Challenge(_Wire):
    message: str  # EIP-4361 text for the owner's wallet to personal_sign
    nonce: str
    expires_at: datetime


class ReviewRequest(_Wire):
    """POST /drafts/{id}/review.

    The moderator sends ``Authorization: Bearer <token>`` and ``by``; the owner sends the
    signed challenge (``message`` + ``signature``), and ``by`` is their address.
    """

    role: Literal["moderator", "owner"]
    approve: bool
    note: str = Field(default="", max_length=2000)
    by: str | None = Field(default=None, max_length=128)
    message: str | None = None
    signature: str | None = None


class PublishRequest(_Wire):
    price: int = Field(default=0, ge=0)  # wei
    deadline: int | None = None  # unix seconds; default now + the server's voucher TTL


WIRE_MODELS: tuple[type[BaseModel], ...] = (
    Health,
    TemplateInfo,
    FrameInfo,
    BioInfo,
    CreateDraft,
    DraftList,
    DraftDetail,
    ChallengeRequest,
    Challenge,
    ReviewRequest,
    PublishRequest,
    PublishResult,
)
