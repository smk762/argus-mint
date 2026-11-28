"""Card drafts and image review: the human approval step between rendering and publishing.

A rendered card is first a *draft*: the unframed art, the finished card image (framed, or
the art itself) and ``draft.json``, kept in a local directory. Nothing is pinned or signed
yet. Before it can be published, the moderator **and** the card's owner each review the
image. Each decision is bound to the image's sha256: a rejection is final for that image,
and a changed image (a corrupted file, or an edit of the card's text, see
:func:`argus_mint.workflow.edit_text`) needs both approvals again. Earlier reviews stay in
the history but no longer count.

With auto-moderation on, the moderator's approval can come from the platform's checks
(:mod:`argus_mint.moderation`): recorded as an ordinary review ``by`` ``auto: ...``. The owner
always reviews in person.

:func:`check_publishable` is the gate ``argus-mint publish`` runs before pinning or signing.
Reviews stay in the draft file; they are not written into the public metadata.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from argus_cortex.hashing import sha256_hex
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from argus_mint._codec import Bytes32
from argus_mint.metadata import (
    BackgroundRef,
    BioRef,
    CardStyle,
    FileRef,
    FrameRef,
    InpaintRef,
    LoraUse,
    Provenance,
    TeaserProvenance,
    teaser_asset_id,
)
from argus_mint.templates import RenderRequest

__all__ = [
    "REVIEWERS",
    "AutoCheck",
    "BioRecord",
    "Edit",
    "CardDraft",
    "IdentityCheck",
    "IdentityOutcome",
    "DraftNotFound",
    "DraftStore",
    "DraftError",
    "NotApproved",
    "Post",
    "Review",
    "ScoreBox",
    "ScorerFile",
    "TeaserFile",
    "TeaserRecord",
    "check_publishable",
    "load_draft",
    "review",
    "save_draft",
    "write_atomic",
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


class BioRecord(BaseModel):
    """The bio written for this card: its template and model, the text, and every attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: BioRef
    text: str
    attempts: tuple[dict[str, Any], ...]
    edited: bool = False  # a person changed the text after the model wrote it (so no model screened it)


class Edit(BaseModel):
    """A change to the card's text, which redraws the card image on the same art."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Role
    by: str = Field(min_length=1, max_length=128)
    at: datetime
    fields: tuple[str, ...]  # "name", "bio", "texts.<slot>"
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # the new card image


class ScoreBox(BaseModel):
    """The part of an image proof scored, in the image's own pixels."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class ScorerFile(BaseModel):
    """The weights a proof scorer loaded, pinned by sha256 (proof's ``ModelRef``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


IdentityOutcome = Literal["passed", "below", "uncalibrated", "skipped", "unavailable", "error"]


class IdentityCheck(BaseModel):
    """argus-proof's score of a teaser's figure against its identity's held-out references (#51).

    ``outcome``: ``passed`` (``score`` >= ``min_score``), ``below`` (under it), ``uncalibrated``
    (scored, but the identity has no ``min_score`` yet), ``skipped`` (nothing to score: a
    viewpoint identity, or an embodied one with no ``proof_export`` yet), ``unavailable`` (proof
    is unreachable, answered 503, or isn't configured) or ``error`` (proof refused the request,
    or its answer can't be read). ``reason`` says it in words. Only ``passed`` (or a viewpoint's
    ``skipped``) lets auto-moderation stand in for the moderator; ``below``, ``unavailable``,
    ``error`` and an embodied ``skipped`` hold the draft.

    ``art_sha256`` is the unframed art that was scored, ``crop`` the scene's ``character_area``
    in its pixels; ``scorer``, ``scorer_version`` and ``model_file`` are proof's provenance for
    ``metric``'s scorer, and ``proof_version`` its wire version.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: datetime
    art_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    presence: Literal["embodied", "viewpoint"]
    outcome: IdentityOutcome
    reason: str = Field(min_length=1)
    metric: Literal["identity", "likeness"] | None = None
    score: float | None = None
    min_score: float | None = None
    export: str | None = None
    crop: ScoreBox | None = None
    proof_version: str | None = None
    scorer: str | None = None
    scorer_version: str | None = None
    model_file: ScorerFile | None = None

    @property
    def held(self) -> bool:
        """Does this outcome hold the draft for a person (whatever auto-moderation is set to)?

        An allow-list, so an outcome added later holds by default. ``uncalibrated`` isn't held
        here (its score is only recorded), but :func:`argus_mint.moderation.auto_check` never
        lets it stand in for the moderator.
        """
        if self.outcome == "skipped":
            return self.presence != "viewpoint"
        return self.outcome not in ("passed", "uncalibrated")


class AutoCheck(BaseModel):
    """What the platform's checks found for the current image. ``passed`` recorded the moderator's approval.

    ``passed`` is only ever true with auto-moderation on: with it off, a teaser's checks (and its
    ``identity`` check) are still recorded, held for a person with the reason that it is off.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: datetime
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    passed: bool
    checks: tuple[str, ...]  # what was checked, in words
    held: tuple[str, ...] = ()  # why a person must look instead
    identity: IdentityCheck | None = None  # a teaser's identity check (argus-proof's score of its figure)


class Post(BaseModel):
    """One posting of a draft's image, as reported by whoever posted it (argus-herald).

    ``sha256`` is of the bytes that were posted, and must be the draft's ``image_sha256``: a teaser
    PNG is baked with no metadata chunks, so a copy stripped of metadata hashes the same.
    ``by`` says which token recorded it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    where: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,31}$")  # the platform, e.g. "bluesky"
    url: str | None = Field(default=None, max_length=2048)  # the post itself, when there is one
    at: AwareDatetime  # when it was posted
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recorded_at: datetime
    by: Literal["poster", "moderator"]

    @field_validator("url")
    @classmethod
    def _http(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("https://", "http://")):
            raise ValueError("a post's url is an http(s) URL")
        return v


class TeaserFile(BaseModel):
    """A reviewed project file a teaser was made from, by id, version and hash (0x keccak256)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    version: int = Field(ge=1)
    hash: str = Field(pattern=r"^0x[0-9a-f]{64}$")


class TeaserBackground(BaseModel):
    """The approved background a teaser was made on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TeaserRecord(BaseModel):
    """What a teaser draft was made from: its project, number, each linked file and the background.

    The frame is the draft's own ``frame``. The quote, attribution and label drawn on it are the
    draft's ``texts`` (by slot), from the reviewed line and project, never edited.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    project: TeaserFile
    links: TeaserFile  # the version of the teaser links it was made from (id = the project's)
    number: str = Field(pattern=r"^T[1-9][0-9]{0,4}$")
    line: TeaserFile
    identity: TeaserFile
    scene: TeaserFile
    presence: Literal["embodied", "viewpoint"]
    background: TeaserBackground
    alt: str = Field(min_length=1, max_length=1000)
    # The reviewed pose an embodied figure was painted in; None: no pose (left out of the document).
    pose: TeaserFile | None = None

    @model_validator(mode="after")
    def _pose_needs_a_figure(self) -> TeaserRecord:
        if self.pose is not None and self.presence != "embodied":
            raise ValueError("a pose is for a figure painted into the image: a viewpoint teaser has none")
        return self

    @model_serializer(mode="wrap")
    def _no_unset_pose(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        doc = handler(self)
        if doc.get("pose") is None:
            doc.pop("pose", None)
        return doc


class CardDraft(BaseModel):
    """Everything needed to publish a card, except the pins and the signature.

    ``kind`` is ``card`` (a game card: ``asset_id``, ``consumer``, maybe a bio) or ``teaser``
    (an image and quote from a teaser project: ``teaser`` records what made it, and it has no
    asset id, consumer or bio). Both are reviewed the same way; ``to`` is the owner who signs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal[1] = 1
    kind: Literal["card", "teaser"] = "card"
    request: RenderRequest  # the request that produced the art (its seed is the final seed)
    attempts: tuple[dict[str, Any], ...]
    renderer: str
    asset_id: Bytes32 | None = None  # a card's; a teaser has none
    to: str
    name: str = Field(min_length=1)
    description: str = ""
    external_url: str | None = None
    consumer: str | None = Field(default=None, min_length=1, max_length=64)  # a card's; a teaser has none
    teaser: TeaserRecord | None = None  # what a teaser was made from
    frame: FrameRef | None = None
    bio: BioRecord | None = None
    texts: dict[str, str] = Field(default_factory=dict)  # consumer display text by frame slot (class, stats...)
    style: CardStyle | None = None  # the owner's styling of the frame
    library: str | None = None  # the pre-rendered library entry the art came from, if any
    # What made the draft, at creation: {"argus-mint": <version>, "darkroom": <version it reported>}.
    # darkroom only for drafts rendered here (not library or supplied art). Empty on older drafts.
    made_with: dict[str, str] = Field(default_factory=dict)
    edits: tuple[Edit, ...] = ()
    auto_check: AutoCheck | None = None
    art_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # the finished card image (== art when unframed)
    created_at: datetime
    reviews: tuple[Review, ...] = ()

    @model_validator(mode="after")
    def _kind_fits(self) -> CardDraft:
        if self.kind == "card":
            if self.asset_id is None or self.consumer is None:
                raise ValueError("a card draft needs its asset_id and consumer")
            if self.teaser is not None:
                raise ValueError("a card draft has no teaser record")
        else:
            if self.teaser is None or self.frame is None:
                raise ValueError("a teaser draft needs its teaser record and frame")
            if self.asset_id is not None or self.consumer is not None or self.bio is not None:
                raise ValueError("a teaser draft has no asset_id, consumer or bio")
            if self.library is not None or self.style is not None:
                raise ValueError("a teaser draft takes no library entry or owner style")
        return self

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

    def mint_asset_id(self) -> bytes:
        """The asset id a voucher names: a card's own, or a teaser's derived one (:func:`teaser_asset_id`)."""
        if self.kind == "teaser":
            assert self.teaser is not None
            return teaser_asset_id(self.teaser.project.id, self.teaser.number)
        assert self.asset_id is not None
        return self.asset_id

    def teaser_provenance(self, art_cid: str) -> TeaserProvenance:
        """A teaser's provenance: its record (each linked file, the background), the frame, the
        unframed art and, for an embodied identity, the inpainting request."""
        if self.kind != "teaser" or self.teaser is None or self.frame is None:
            raise DraftError("only a teaser has teaser provenance")
        t, req = self.teaser, self.request

        def ref(f: TeaserFile) -> FileRef:
            return FileRef(id=f.id, version=f.version, hash=f.hash)

        inpaint = None
        if t.presence == "embodied":
            if req.init_image is None or req.init_image.mask_sha256 is None:
                raise DraftError("an embodied teaser's request names no inpainting input")
            inpaint = InpaintRef(
                seed=req.seed,
                attempts=len(self.attempts),
                model=req.model,
                loras=tuple(LoraUse(name=x.name, weight=x.weight) for x in req.loras) or None,
                image_sha256=req.init_image.sha256,
                mask_sha256=req.init_image.mask_sha256,
                denoise=req.init_image.denoise,
            )
        return TeaserProvenance(
            project=ref(t.project),
            links=ref(t.links),
            number=t.number,
            line=ref(t.line),
            identity=ref(t.identity),
            scene=ref(t.scene),
            presence=t.presence,
            background=BackgroundRef(id=t.background.id, sha256=t.background.sha256),
            frame=self.frame,
            art=f"ipfs://{art_cid}",
            asset_id=teaser_asset_id(t.project.id, t.number),
            inpaint=inpaint,
            pose=ref(t.pose) if t.pose is not None else None,
        )

    def provenance(self, art_cid: str | None) -> Provenance:
        """The card's provenance. ``art_cid`` (the unframed render) is required for a framed card."""
        if self.kind != "card" or self.asset_id is None or self.consumer is None:
            raise DraftError("a teaser has no card provenance: its metadata is built from teaser_provenance")
        return Provenance.for_render(
            self.request,
            attempts=len(self.attempts),
            asset_id=self.asset_id,
            consumer=self.consumer,
            frame=self.frame,
            art=f"ipfs://{art_cid}" if self.frame is not None and art_cid else None,
            bio=self.bio.ref if self.bio is not None else None,
            style=self.style,
        )


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` by rename: a concurrent reader sees the old file or the new one, never part of one."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    """Persist a rename in ``directory`` (best effort: not every platform can open a directory)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


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
        raise DraftError("this image was rejected; edit the card's text or render a new draft")
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
    write_atomic(directory / DRAFT_FILE, draft.model_dump_json(indent=2) + "\n")


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


PUBLISHED_FILE = "publish.json"
POSTS_FILE = "posts.json"
_DRAFT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


class DraftStore:
    """Drafts as directories under ``root``, addressed by id (the HTTP server's view).

    Ids are validated before they touch the filesystem, so a request can't name a path.
    A published draft also holds ``publish.json``: the pins, metadata and signed voucher. A posted
    one holds ``posts.json``: each post, in the order recorded.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    def new_id(self) -> str:
        return secrets.token_hex(8)

    def path(self, draft_id: str) -> Path:
        if not _DRAFT_ID.fullmatch(draft_id):
            raise DraftError(f"not a draft id: {draft_id!r}")
        return self.root / draft_id

    def exists(self, draft_id: str) -> bool:
        return (self.path(draft_id) / DRAFT_FILE).is_file()

    def ids(self) -> list[str]:
        """Every draft id, newest first."""
        if not self.root.is_dir():
            return []
        found = [p for p in self.root.iterdir() if _DRAFT_ID.fullmatch(p.name) and (p / DRAFT_FILE).is_file()]
        return [p.name for p in sorted(found, key=lambda p: (p / DRAFT_FILE).stat().st_mtime, reverse=True)]

    def load(self, draft_id: str) -> tuple[CardDraft, bytes, bytes]:
        if not self.exists(draft_id):
            raise DraftNotFound(f"no draft {draft_id}")
        return load_draft(self.path(draft_id))

    def save(self, draft_id: str, draft: CardDraft, art: bytes | None = None, image: bytes | None = None) -> None:
        save_draft(self.path(draft_id), draft, art, image)

    def published(self, draft_id: str) -> dict[str, Any] | None:
        f = self.path(draft_id) / PUBLISHED_FILE
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None

    def save_published(self, draft_id: str, result: dict[str, Any]) -> None:
        f = self.path(draft_id) / PUBLISHED_FILE
        write_atomic(f, json.dumps(result, indent=2, ensure_ascii=False) + "\n")

    def posts(self, draft_id: str) -> tuple[Post, ...]:
        f = self.path(draft_id) / POSTS_FILE
        if not f.is_file():
            return ()
        try:
            return tuple(Post.model_validate(p) for p in json.loads(f.read_text(encoding="utf-8"))["posts"])
        except (ValueError, KeyError, TypeError, ValidationError) as e:
            raise DraftError(f"{f}: {e}") from e

    def add_post(self, draft_id: str, post: Post) -> tuple[Post, ...]:
        """Append ``post`` (the caller holds the drafts lock). Returns every post of the draft."""
        posts = (*self.posts(draft_id), post)
        doc = {"posts": [p.model_dump(mode="json") for p in posts]}
        write_atomic(self.path(draft_id) / POSTS_FILE, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        return posts


class DraftNotFound(DraftError):
    """No draft with that id."""
