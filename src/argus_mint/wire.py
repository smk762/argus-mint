"""The HTTP wire contract of ``argus-mint serve``: request and response models.

argus-studio and argus-assay generate their TypeScript types from the committed schema
(``schema/mint-wire.schema.json``, written by ``argus-mint schema``), pinned by release tag
and sha256. Bump :data:`WIRE_VERSION`'s major on any breaking change; a client refuses a
major it doesn't know.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from argus_mint.backgrounds import Background, BackgroundSet
from argus_mint.bios import BioTemplate
from argus_mint.cards import CardDraft
from argus_mint.frames import Frame
from argus_mint.metadata import CardStyle
from argus_mint.templates import LoraChoice, Template
from argus_mint.training import TrainingRecord

__all__ = [
    "WIRE_VERSION",
    "BackgroundDecision",
    "IdentityTrainingView",
    "BackgroundSetList",
    "CreateTeaserDraft",
    "ProjectTracker",
    "TeaserDraftState",
    "TeaserProgress",
    "TeaserStages",
    "BioInfo",
    "Capabilities",
    "Challenge",
    "ChallengeRequest",
    "CreateDraft",
    "DraftDetail",
    "DraftList",
    "DraftSummary",
    "EditTextRequest",
    "FrameLayout",
    "LayoutRequest",
    "LibraryItem",
    "LibraryListing",
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
    "TemplateCheck",
    "SampleItem",
    "SampleJob",
    "SampleJobList",
    "SampleRequest",
    "ProjectChecks",
    "ProjectDetail",
    "ProjectFile",
    "ProjectList",
    "ProjectSummary",
    "SaveProjectFile",
    "SavedProjectFile",
    "TextCheck",
]

# 1.1 samples, previews, catalogue; 1.2 auto-moderation; 1.3 owner edits, style; 1.4 checkpoints, library;
# 1.5 LoRA choices, sampling per checkpoint family; 1.6 provenance records variables, checkpoint, LoRAs;
# 1.7 templates checked against darkroom's workflows; 1.8 sampling per checkpoint;
# 1.9 drafts record what made them (made_with); 1.10 slot texts are bounded (SlotTexts);
# 1.11 teaser projects (authoring, checks, review); 1.12 backgrounds, teaser drafts (CardDraft.kind), the tracker;
# 1.14 identity training (curator, lens, forge, proof), Identity.proof_export/check_metric/min_score/training
WIRE_VERSION = "1.14"

MAX_SLOTS = 16  # text slots a request may fill; frames have a handful
# Display text by frame slot: slot names as frames name them, each text as long as a bio may be.
SlotTexts = Annotated[
    dict[
        Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")],
        Annotated[str, StringConstraints(max_length=2000)],
    ],
    Field(max_length=MAX_SLOTS),
]
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
    library: bool = False  # pre-rendered portraits are available (GET /library)
    edit_text: bool = False  # owner edits of text and style: not read-only, and owner sign-in is set up
    auto_moderate: bool = False  # the platform's checks approve as the moderator when they all pass
    check_templates: bool = False  # darkroom can be asked whether it renders a template (GET .../check)
    author_catalogue: bool = False  # teaser project files can be written here: token set, projects dir writable
    backgrounds: bool = False  # scene backgrounds can be rendered: darkroom configured, token set, not read-only
    teasers: bool = False  # teaser drafts: moderator token set, not read-only (embodied ones need darkroom too)
    # an embodied identity's LoRA can be trained here: darkroom, curator, lens, forge and proof configured,
    # the shared exports and LoRA directories set, moderator token set, not read-only
    identity_training: bool = False


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
    """POST /drafts. The template, frame and bio are named by id and version, never by path.

    Either render (``variables``, ``seed``, optionally ``model``), or take a pre-rendered
    ``library`` entry, whose variables, seed and checkpoint then apply.
    """

    template: Ref
    variables: dict[str, str] = Field(default_factory=dict)
    seed: int = Field(default=0, ge=0, le=2**32 - 1)
    model: str | None = None  # a checkpoint the template allows; default: the template's own
    loras: list[LoraChoice] = Field(default_factory=list)  # from the template's lora_options
    library: str | None = None  # a library entry id instead of rendering
    asset: str | None = Field(default=None, min_length=1, max_length=256)  # asset id = keccak256(utf8)
    asset_id: str | None = None  # 0x + 64 hex
    to: str
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    external_url: str | None = None
    frame: Ref | None = None
    bio: Ref | None = None
    texts: SlotTexts = Field(default_factory=dict)  # consumer display text by frame slot
    style: CardStyle | None = None  # the owner's choices, if the consumer collected them up front


class TeaserOf(_Wire):
    project: str
    number: str


class DraftSummary(_Wire):
    id: str
    kind: Literal["card", "teaser"] = "card"
    name: str
    to: str
    status: DraftStatus
    awaiting: list[str]
    created_at: datetime
    template: Ref
    frame: Ref | None
    image_sha256: str
    teaser: TeaserOf | None = None  # a teaser's project and number


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
    """The owner's change to the card: text and style. ``None`` leaves a field as it is; an empty
    slot text clears it; ``style`` replaces the whole style (an empty one clears it)."""

    name: str | None = Field(default=None, max_length=120)
    bio: str | None = Field(default=None, max_length=2000)
    texts: SlotTexts | None = None
    style: CardStyle | None = None


class ChallengeRequest(_Wire):
    """POST /drafts/{id}/challenge: the owner asks for the message to sign.

    ``action: review`` with ``approve``; ``action: edit`` with the exact ``edit`` it will authorise.
    """

    action: Literal["review", "edit"] = "review"
    approve: bool = True
    edit: TextEdit | None = None


class EditTextRequest(TextEdit):
    """POST /drafts/{id}/edit: only the owner edits, with the signed challenge for exactly this edit.

    The moderator approves or rejects; they don't change the card.
    """

    message: str
    signature: str


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
    model: str | None = None  # a checkpoint the template allows; default: the template's own
    loras: list[LoraChoice] = Field(default_factory=list)  # from the template's lora_options
    count: int = Field(default=4, ge=1, le=12)
    variables: dict[str, str] = Field(default_factory=dict)  # fixed values; the rest vary
    seed: int = Field(default=0, ge=0, le=2**32 - 1)  # picks the combinations, and the first render seed
    name: str = Field(default="Sample", min_length=1, max_length=120)
    texts: SlotTexts = Field(default_factory=dict)
    style: CardStyle | None = None


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
    texts: SlotTexts = Field(default_factory=dict)
    style: CardStyle | None = None


class CatalogueDecision(_Wire):
    """POST /{templates,frames,bios}/{id}/{version}/review (moderator token)."""

    decision: Literal["approve", "retire"]
    by: str = Field(min_length=1, max_length=128)


class TemplateCheck(_Wire):
    """GET /templates/{id}/{version}/check: what would make darkroom refuse to render the template.

    Checked against darkroom's model list: checkpoints, LoRAs, sampler and scheduler exist, and a
    workflow renders each checkpoint with every LoRA count the template allows, taking what the
    template sends. Approving a template is refused while ``problems`` isn't empty.
    """

    template: Ref
    ok: bool
    problems: list[str]


class CatalogueEntry(_Wire):
    kind: Literal["template", "frame", "bio"]
    id: str
    version: int
    status: Literal["draft", "approved", "retired"]
    hash: str
    file: str


class LayoutRequest(_Wire):
    """POST /frames/{id}/{version}/layout: where these texts go, for a browser preview."""

    texts: SlotTexts


class LineLayout(_Wire):
    text: str
    x: int  # left edge, overlay pixels
    y: int  # top edge (the font's ascender line), overlay pixels


class SlotLayout(_Wire):
    font: str | None  # GET /frames/{id}/{version}/fonts/{font}; None: Pillow's built-in font
    size: int
    ascent: int
    descent: int
    effects: dict[str, int]  # shadow offset, outline width, glow blur radius, in pixels
    lines: list[LineLayout]


class FrameLayout(_Wire):
    width: int
    height: int
    slots: dict[str, SlotLayout]


class LibraryItem(_Wire):
    id: str
    group: int  # items in a group share variables and seed, one per checkpoint
    model: str
    variables: dict[str, str]
    seed: int
    sha256: str


class LibraryListing(_Wire):
    """GET /library: pre-rendered portraits, for drafts without a GPU (``CreateDraft.library``)."""

    template: Ref
    models: list[str]
    count: int
    items: list[LibraryItem]


# -- teaser projects --------------------------------------------------------------------------------

ProjectKind = Literal["project", "identity", "scene", "line", "teasers"]


class TextCheck(_Wire):
    """SingGuard's verdict on one text, under the project's policy (darkroom's text pre-flight).

    The verdict is stored in the file's ``guard`` block and holds while the text and the policy
    are the ones it judged."""

    field: str  # which file and field, e.g. "scene lamp-room v1: prompt"
    passed: bool
    detail: str = ""  # the rule or reason when it didn't pass


class ProjectChecks(_Wire):
    """What stands between a project's files and approval.

    ``spoiler_rules`` are numbers in the private spoiler list, never its terms. ``guard_ran`` is
    false when no darkroom is configured: then nothing can be approved.
    """

    problems: list[str]  # schema and cross-file problems, as sentences
    spoiler_rules: list[int]
    guard: list[TextCheck]
    guard_ran: bool


class ProjectFile(_Wire):
    kind: ProjectKind
    id: str
    version: int
    status: Literal["draft", "approved", "retired"]
    hash: str
    file: str  # relative to the project's directory
    doc: dict[str, Any]  # the file's content


class ProjectSummary(_Wire):
    id: str
    title: str
    status: Literal["draft", "approved", "retired"]


class ProjectList(_Wire):
    projects: list[ProjectSummary]


class ProjectDetail(_Wire):
    """GET /projects/{id}: every file of a project (teaser links included, kind ``teasers``) and their checks.

    ``checks.guard`` holds the verdicts stored in the files that still hold; a text with none
    hasn't been checked since it (or the project's policy) last changed.
    """

    id: str
    files: list[ProjectFile]
    checks: ProjectChecks


class SaveProjectFile(_Wire):
    """POST /projects/{id}/files/{kind}/{item}/{version}: write a draft file.

    ``doc`` is the file's content without ``status``, ``approved_by`` or ``approved_at``: a saved
    file is always a draft, and ``guard`` is written by the server alone. An approved or
    retired file never changes; save a new version.
    """

    doc: dict[str, Any]


class SavedProjectFile(_Wire):
    file: ProjectFile
    checks: ProjectChecks  # for the saved file's texts, and the project's cross-file problems


class BackgroundSetList(_Wire):
    """GET /projects/{id}/backgrounds: each scene version's backgrounds, by scene then version."""

    sets: list[BackgroundSet]


class BackgroundDecision(_Wire):
    """POST /projects/{id}/backgrounds/{bg}/review: the moderator's decision on one background."""

    decision: Literal["approve", "reject"]
    by: str = Field(min_length=1, max_length=128)
    note: str = Field(default="", max_length=2000)


class TeaserStages(_Wire):
    """The stages a teaser has reached, as argus-mint works them out (a client never does)."""

    quote_final: bool  # its line is approved
    concept_chosen: bool  # its identity and scene are approved
    image_made: bool  # a draft made from the current line, identity and scene is approved or published
    posted: bool = False  # reserved: posting teasers comes later (#52)
    minted: bool = False  # reserved: minting teasers comes later (#52)


class TeaserDraftState(_Wire):
    """One teaser draft, as the tracker lists it. ``current``: made from the current links' line,
    identity and scene (an older draft of the same number may not be)."""

    id: str
    status: DraftStatus
    awaiting: list[str]
    created_at: datetime
    links_version: int
    background: str
    current: bool


class TeaserProgress(_Wire):
    number: str
    stages: TeaserStages
    background: str | None  # the chosen background, if any
    drafts: list[TeaserDraftState]  # newest first


class ProjectTracker(_Wire):
    """GET /projects/{id}/tracker: each teaser of the newest approved links (none: ``links_version`` null)."""

    project: str
    links_version: int | None
    teasers: list[TeaserProgress]


class CreateTeaserDraft(_Wire):
    """POST /projects/{id}/teasers/{n}/draft. Everything else comes from the project's reviewed files."""

    to: str  # the owner, who signs the review


TrainingStepStatus = Literal["not_started", "queued", "running", "done", "failed"]
TrainingStep = Literal["candidates", "select", "caption", "train", "evaluate", "pin"]


class IdentityTrainingView(_Wire):
    """GET /projects/{id}/identities/{identity}/{version}/training: the record, and each step's status
    as mint works it out (a job a restart cut off reads as failed). ``next``: the one step the
    moderator may start now, if any."""

    project: str
    identity: str
    version: int
    steps: dict[TrainingStep, TrainingStepStatus]
    next: TrainingStep | None
    record: TrainingRecord | None


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
    TemplateCheck,
    LayoutRequest,
    FrameLayout,
    LibraryListing,
    ProjectList,
    ProjectDetail,
    SaveProjectFile,
    SavedProjectFile,
    BackgroundSet,
    BackgroundSetList,
    Background,
    BackgroundDecision,
    CreateTeaserDraft,
    ProjectTracker,
    IdentityTrainingView,
)
