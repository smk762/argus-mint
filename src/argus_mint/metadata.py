"""Canonical ERC-721 card metadata (OpenSea-compatible JSON). Pure; no I/O.

The consumer decides the attributes; argus-mint only checks their types. Metadata
describes a record and never carries in-game power (hard rule 4).

Two parts of a card's metadata never change after mint: ``image`` and ``provenance``.
:func:`content_hash` covers exactly those two, and is what a mint voucher's
``contentHash`` commits to, so the commitment still checks out after the consumer
refreshes live attributes.

A teaser minted as a Card has the same document, with a :class:`TeaserProvenance` (``kind:
"teaser"``) in place of a card's :class:`Provenance`, and an asset id derived from its project
and number (:func:`teaser_asset_id`). A card's document is unchanged by teasers.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_serializer,
    field_validator,
    model_validator,
)

from argus_mint._codec import Bytes32, canonical, hex0x, keccak256

if TYPE_CHECKING:
    from argus_mint.templates import RenderRequest

__all__ = [
    "Attribute",
    "BackgroundRef",
    "BioRef",
    "FileRef",
    "InpaintRef",
    "TeaserProvenance",
    "teaser_asset_id",
    "CardStyle",
    "FrameRef",
    "LoraUse",
    "SlotBackground",
    "SlotStyle",
    "Tint",
    "Provenance",
    "canonical",
    "card_metadata",
    "content_hash",
    "ipfs_uri",
]

# CIDv0 (base58btc, "Qm…") or CIDv1 in base32 lower case ("b…", e.g. "bafy…").
_CID = re.compile(r"^(Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{50,})$")
_NUMERIC_DISPLAY = {"number", "boost_number", "boost_percentage", "date"}


def ipfs_uri(cid: str) -> str:
    """``ipfs://<cid>`` for a bare CID; rejects anything that doesn't look like one."""
    if not isinstance(cid, str) or not _CID.fullmatch(cid):
        raise ValueError(f"not a CID: {cid!r} (pass the bare CID, without ipfs://)")
    return f"ipfs://{cid}"


class Attribute(BaseModel):
    """One OpenSea ``attributes`` entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trait_type: str = Field(min_length=1, max_length=64)
    value: StrictStr | StrictInt | StrictFloat
    display_type: Literal["number", "boost_number", "boost_percentage", "date"] | None = None

    @field_validator("value")
    @classmethod
    def _finite(cls, v: Any) -> Any:
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("value must be finite")
        return v

    @model_validator(mode="after")
    def _display_matches_value(self) -> Attribute:
        if self.display_type in _NUMERIC_DISPLAY and isinstance(self.value, str):
            raise ValueError(f"display_type {self.display_type!r} needs a numeric value")
        return self

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class FrameRef(BaseModel):
    """The reviewed frame composited onto the render (see :mod:`argus_mint.frames`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    hash: Bytes32


_HEX = r"^#[0-9a-fA-F]{6}$"


def _whole_as_int(x: float) -> int | float:
    """A whole float as an int, for JSON that's hashed.

    Provenance is hashed as canonical JSON, and JSON libraries disagree on a whole float: Python
    writes ``1.0``, JavaScript ``1``. Written as an int, every verifier hashes the same bytes.
    """
    # Only within the range JavaScript writes as a plain integer: from 1e21 it writes 1e+21.
    if not (x.is_integer() and abs(x) < 1e21):
        return x
    if abs(x) <= 2**53:  # exact, and written the same by both
        return int(x)
    # Above 2^53 JavaScript writes the shortest round-trip digits padded with zeros (2**60 is
    # 1152921504606847000), not the exact binary value; Python's repr has the same shortest digits.
    return int(Decimal(repr(x)))


class Tint(BaseModel):
    """Recolour the frame overlay: every pixel moves toward ``colour`` at its own brightness."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    colour: str = Field(pattern=_HEX)
    strength: float = Field(default=0.6, ge=0.0, le=1.0)

    @field_serializer("strength", when_used="json")
    def _strength_json(self, x: float) -> int | float:
        return _whole_as_int(x)


class SlotBackground(BaseModel):
    """A filled, rounded box behind a text slot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    colour: str = Field(pattern=_HEX)
    opacity: float = Field(default=0.6, ge=0.0, le=1.0)
    radius: int = Field(default=8, ge=0, le=200)
    padding: int = Field(default=4, ge=0, le=100)

    @field_serializer("opacity", when_used="json")
    def _opacity_json(self, x: float) -> int | float:
        return _whole_as_int(x)


class SlotStyle(BaseModel):
    """How one text slot is drawn. Unset fields keep the frame's own choices."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    colour: str | None = Field(default=None, pattern=_HEX)
    effect: Literal["none", "shadow", "outline", "glow"] = "none"
    effect_colour: str = Field(default="#000000", pattern=_HEX)
    background: SlotBackground | None = None


class CardStyle(BaseModel):
    """The owner's styling of a framed card: any colours, so be it if that's black on black."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tint: Tint | None = None
    slots: dict[str, SlotStyle] = Field(default_factory=dict)

    def is_plain(self) -> bool:
        return self.tint is None and not self.slots


class LoraUse(BaseModel):
    """A LoRA the render used, with its weight (its trigger words come from the template)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    weight: float

    @field_serializer("weight", when_used="json")
    def _weight_json(self, x: float) -> int | float:
        return _whole_as_int(x)


class BioRef(BaseModel):
    """The reviewed bio template (and model) that wrote the card's bio (see :mod:`argus_mint.bios`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    hash: Bytes32
    model: str = Field(min_length=1)


class Provenance(BaseModel):
    """How the card's image was made. Fixed at mint; covered by :func:`content_hash`.

    ``frame`` and ``art`` are set only for a framed card: the frame, and ``ipfs://`` of the
    unframed render. ``bio`` names the bio template and model when a bio was written (the
    text itself is the card's ``description`` and is drawn on the image). ``style`` is the
    owner's styling of the frame, so the card can be redrawn exactly.

    ``variables``, ``model`` and ``loras`` are the render's choices. With the template (pinned
    by its hash) and ``seed`` they fix the render request, so anyone can rebuild it (hard rule 7).
    The variables are values from the template's fixed lists, never free text (hard rule 1).

    Unset fields are left out of the JSON, so a card made before a field existed serializes (and
    hashes) exactly as it did.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    template: str = Field(min_length=1)
    template_version: int = Field(ge=1)
    template_hash: Bytes32
    seed: int = Field(ge=0, le=2**32 - 1)
    attempts: int = Field(ge=1)
    asset_id: Bytes32
    consumer: str = Field(min_length=1, max_length=64)
    frame: FrameRef | None = None
    art: str | None = None
    bio: BioRef | None = None
    style: CardStyle | None = None  # the owner's styling of the frame, when they chose any
    variables: dict[str, str] | None = None  # the template variables' values
    model: str | None = None  # the checkpoint
    loras: tuple[LoraUse, ...] | None = None  # every LoRA applied (the template's own, then chosen ones); None: none

    @classmethod
    def for_render(cls, req: RenderRequest, **fields: Any) -> Provenance:
        """The provenance of a render of ``req``: its template, seed and choices, plus ``fields``."""
        return cls(
            template=req.template_id,
            template_version=req.template_version,
            template_hash=req.template_hash,
            seed=req.seed,
            variables=dict(sorted(req.variables.items())) or None,
            model=req.model,
            loras=tuple(LoraUse(name=x.name, weight=x.weight) for x in req.loras) or None,
            **fields,
        )

    @field_validator("art")
    @classmethod
    def _art_uri(cls, v: str | None) -> str | None:
        if v is not None:
            if not v.startswith("ipfs://"):
                raise ValueError(f"art must be an ipfs:// URI, not {v!r}")
            ipfs_uri(v.removeprefix("ipfs://"))  # validates the CID
        return v

    @model_validator(mode="after")
    def _frame_and_art(self) -> Provenance:
        if (self.frame is None) != (self.art is None):
            raise ValueError("frame and art go together: a framed card records its unframed render")
        return self

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


TEASER_ASSET_PREFIX = "argus-mint/teaser"


def teaser_asset_id(project: str, number: str) -> bytes:
    """A teaser's asset id: ``keccak256(utf8("argus-mint/teaser/<project>/<T n>"))``.

    The same for every draft of that teaser, so a teaser mints once on a Card contract, as a
    card's asset does. (A card's asset id is the consumer's, often ``keccak256(utf8(key))``.)
    """
    if not project or "/" in project or not number.startswith("T"):
        raise ValueError(f"not a project and teaser number: {project!r} {number!r}")
    return keccak256(f"{TEASER_ASSET_PREFIX}/{project}/{number}".encode())


class FileRef(BaseModel):
    """A reviewed project file a teaser was made from: its id, version and keccak256 hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    hash: Bytes32


class BackgroundRef(BaseModel):
    """The approved background a teaser was made on, by id and the sha256 of its PNG."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class InpaintRef(BaseModel):
    """How an embodied identity was painted onto the background: the identity file is the
    request's template, and this records the rest of the request (hard rule 7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int = Field(ge=0, le=2**32 - 1)  # the final seed
    attempts: int = Field(ge=1)
    model: str = Field(min_length=1)  # the checkpoint (the scene's)
    loras: tuple[LoraUse, ...] | None = None  # the identity's LoRA, then the project's style LoRA; None: none
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # the input image (the background)
    mask_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # white over the scene's character area
    denoise: float = Field(ge=0.05, le=1.0)

    @field_serializer("denoise", when_used="json")
    def _denoise_json(self, x: float) -> int | float:
        return _whole_as_int(x)


class TeaserProvenance(BaseModel):
    """How a teaser was made: the teaser record, by reviewed file. Fixed at mint; covered by
    :func:`content_hash` exactly as a card's :class:`Provenance` is.

    ``project``, ``links``, ``line``, ``identity``, ``scene`` and, when the figure was painted in
    one, ``pose`` are the reviewed files (id, version, keccak256 hash); ``background`` the approved background's id and PNG sha256;
    ``frame`` the frame; ``art`` ``ipfs://`` of the unframed art (the background with the figure
    painted in, or the background itself for a viewpoint identity). ``inpaint`` is set for an
    embodied identity only. The quote, attribution and label on the image come from the line and
    project, so they aren't repeated here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["teaser"] = "teaser"
    project: FileRef
    links: FileRef
    number: str = Field(pattern=r"^T[1-9][0-9]{0,4}$")
    line: FileRef
    identity: FileRef
    scene: FileRef
    presence: Literal["embodied", "viewpoint"]
    background: BackgroundRef
    frame: FrameRef
    art: str
    asset_id: Bytes32
    inpaint: InpaintRef | None = None
    # The reviewed pose file the figure was painted in (its hash covers the keypoints, the strength and how it
    # was drawn; the scene's covers where). None, and absent from the document: no pose.
    pose: FileRef | None = None

    @field_validator("art")
    @classmethod
    def _art_uri(cls, v: str) -> str:
        if not v.startswith("ipfs://"):
            raise ValueError(f"art must be an ipfs:// URI, not {v!r}")
        ipfs_uri(v.removeprefix("ipfs://"))
        return v

    @model_validator(mode="after")
    def _inpaint_iff_embodied(self) -> TeaserProvenance:
        if (self.presence == "embodied") != (self.inpaint is not None):
            raise ValueError("an embodied teaser records its inpainting, and a viewpoint teaser has none")
        if self.pose is not None and self.presence != "embodied":
            raise ValueError("a pose is for a figure painted into the image: a viewpoint teaser has none")
        if self.asset_id != teaser_asset_id(self.project.id, self.number):
            raise ValueError("a teaser's asset id is derived from its project and number")
        return self

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def card_metadata(
    *,
    name: str,
    description: str,
    image_cid: str,
    external_url: str | None,
    attributes: list[Attribute],
    provenance: Provenance | TeaserProvenance,
) -> dict[str, Any]:
    """Build a token's metadata document (a card's, or a teaser's). Serialize it with :func:`canonical`."""
    if not name:
        raise ValueError("name is required")
    trait_types = [a.trait_type for a in attributes]
    if len(set(trait_types)) != len(trait_types):
        raise ValueError("an attribute trait_type appears twice")
    doc: dict[str, Any] = {
        "name": name,
        "description": description,
        "image": ipfs_uri(image_cid),
        "attributes": [a.to_json() for a in attributes],
        "provenance": provenance.to_json(),
    }
    if external_url is not None:
        if not external_url.startswith(("https://", "http://", "ipfs://")):
            raise ValueError("external_url must be an http(s) or ipfs URL")
        doc["external_url"] = external_url
    return doc


def content_hash(doc: dict[str, Any]) -> bytes:
    """keccak256(canonical({"image", "provenance"})) — the voucher's ``contentHash``.

    Takes a full metadata document (live or frozen) and hashes only its immutable part.
    """
    try:
        core = {"image": doc["image"], "provenance": doc["provenance"]}
    except KeyError as e:
        raise ValueError(f"metadata has no {e.args[0]!r}") from e
    return keccak256(canonical(core))


def content_hash_hex(doc: dict[str, Any]) -> str:
    return hex0x(content_hash(doc))
