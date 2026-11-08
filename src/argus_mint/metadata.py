"""Canonical ERC-721 card metadata (OpenSea-compatible JSON). Pure; no I/O.

The consumer decides the attributes; argus-mint only checks their types. Metadata
describes a record and never carries in-game power (hard rule 4).

Two parts of a card's metadata never change after mint: ``image`` and ``provenance``.
:func:`content_hash` covers exactly those two, and is what a mint voucher's
``contentHash`` commits to, so the commitment still checks out after the consumer
refreshes live attributes.
"""

from __future__ import annotations

import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr, field_validator, model_validator

from argus_mint._codec import Bytes32, canonical, hex0x, keccak256

__all__ = ["Attribute", "Provenance", "canonical", "card_metadata", "content_hash", "ipfs_uri"]

# CIDv0 (base58btc, "Qm…") or CIDv1 in base32 lower case ("b…", e.g. "bafy…").
_CID = re.compile(r"^(Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{50,})$")
_NUMERIC_DISPLAY = {"number", "boost_number", "boost_percentage", "date"}


def ipfs_uri(cid: str) -> str:
    """``ipfs://<cid>`` for a bare CID; rejects anything that doesn't look like one."""
    if not isinstance(cid, str) or not _CID.match(cid):
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


class Provenance(BaseModel):
    """How the card's image was made. Fixed at mint; covered by :func:`content_hash`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    template: str = Field(min_length=1)
    template_version: int = Field(ge=1)
    template_hash: Bytes32
    seed: int = Field(ge=0, le=2**32 - 1)
    attempts: int = Field(ge=1)
    asset_id: Bytes32
    consumer: str = Field(min_length=1, max_length=64)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def card_metadata(
    *,
    name: str,
    description: str,
    image_cid: str,
    external_url: str | None,
    attributes: list[Attribute],
    provenance: Provenance,
) -> dict[str, Any]:
    """Build a card's metadata document. Serialize it with :func:`canonical`."""
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
