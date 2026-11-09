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
    "EditTextRequest",
    "TextEdit",
    "FrameInfo",
    "Health",
    "PublishRequest",
    "PublishResult",
    "Ref",
    "ReviewRequest",
    "TemplateInfo",
    "WIRE_MODELS",
    "ArtSource",
    "CatalogueDecision",
    "CatalogueEntry",
    "FramePreviewRequest",
    "SampleItem",
    "SampleJob",
    "SampleJobList",
    "SampleRequest",
]

WIRE_VERSION = "1.2"  # 1.1: samples, frame previews, catalogue decisions; 1.2: text edits, auto-moderation
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
    sample: bool = False  # darkroom configured, moderator token set, not read-only
    review_catalogue: bool = False  # moderator token set, catalogue files writable, not read-only
    edit_text: bool = False  # not read-only, and a moderator token or owner sign-in is available
    auto_moderate: bool = False  # the platform's checks approve as the moderator when they all pass


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
    file: str  # path relative to the templates directory
    combinations: int  # how many variable combinations a reviewer is approving
    template: Template


class FrameInfo(_Wire):
    hash: str
    file: str
    width: int
    height: int
    frame: Frame


class BioInfo(_Wire):
    hash: str
    file: str
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


class TextEdit(_Wire):
    """A change to the card's text. ``None`` leaves a field as it is; an empty slot text clears it."""

    name: str | None = Field(default=None, max_length=120)
    bio: str | None = Field(default=None, max_length=2000)
    texts: dict[str, str] | None = None


class ChallengeRequest(_Wire):
    """POST /drafts/{id}/challenge: the owner asks for the message to sign.

    ``action: review`` with ``approve``; ``action: edit`` with the exact ``edit`` it will authorise.
    """

    action: Literal["review", "edit"] = "review"
    approve: bool = True
    edit: TextEdit | None = None


class EditTextRequest(TextEdit):
    """POST /drafts/{id}/text: the moderator by token and ``by``; the owner with a signed edit challenge."""

    role: Literal["moderator", "owner"]
    by: str | None = Field(default=None, max_length=128)
    message: str | None = None
    signature: str | None = None


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


class SampleRequest(_Wire):
    """POST /samples: render sample cards of a template (any status) for its reviewer."""

    template: Ref
    frame: Ref | None = None
    bio: Ref | None = None
    count: int = Field(default=4, ge=1, le=12)
    variables: dict[str, str] = Field(default_factory=dict)  # fixed values; the rest vary
    seed: int = Field(default=0, ge=0, le=2**32 - 1)  # picks the combinations, and the first render seed
    name: str = Field(default="Sample", min_length=1, max_length=120)
    texts: dict[str, str] = Field(default_factory=dict)


class SampleItem(_Wire):
    n: int
    variables: dict[str, str]
    prompt: str | None = None
    seed: int | None = None
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    bio: str | None = None
    bio_attempts: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None  # the render failed (every attempt rejected, or darkroom refused)
    bio_error: str | None = None
    art: bool = False  # GET /samples/{id}/{n}/art exists
    framed: bool = False  # GET /samples/{id}/{n}/framed exists


class SampleJob(_Wire):
    id: str
    status: Literal["queued", "running", "done", "failed"]
    created_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    request: SampleRequest
    items: list[SampleItem] = Field(default_factory=list)


class SampleJobList(_Wire):
    items: list[SampleJob]
    total: int


class ArtSource(_Wire):
    """Existing art to preview a frame on: a draft's render, or a sample's."""

    draft: str | None = None
    sample: str | None = None
    n: int | None = None


class FramePreviewRequest(_Wire):
    """POST /frames/{id}/{version}/preview: a frame (any status) on existing art; returns a PNG."""

    art: ArtSource
    name: str = Field(default="Sample", min_length=1, max_length=120)
    bio: str | None = Field(default=None, max_length=2000)
    texts: dict[str, str] = Field(default_factory=dict)


class CatalogueDecision(_Wire):
    """POST /{templates,frames,bios}/{id}/{version}/review (moderator token)."""

    decision: Literal["approve", "retire"]
    by: str = Field(min_length=1, max_length=128)


class CatalogueEntry(_Wire):
    kind: Literal["template", "frame", "bio"]
    id: str
    version: int
    status: Literal["draft", "approved", "retired"]
    hash: str
    file: str


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
    TextEdit,
    EditTextRequest,
    ReviewRequest,
    PublishRequest,
    PublishResult,
    SampleRequest,
    SampleJob,
    SampleJobList,
    FramePreviewRequest,
    CatalogueDecision,
    CatalogueEntry,
)
