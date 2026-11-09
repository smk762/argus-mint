"""Render client: send a filled template to darklight, retry with a new seed when the
safety check rejects the image, and record every attempt.

Hard rule 3: retry, don't loosen. A rejection is retried with the next deterministic
seed, up to the template's ``max_attempts``; the prompt is never changed and the check
is never turned off. darklight's wire API is not settled yet, so the client is the
:class:`Darklight` protocol; the real HTTP client will implement it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import structlog

from argus_mint._codec import Bytes32
from argus_mint.metadata import Provenance
from argus_mint.templates import SEED_MAX, RenderRequest, Template, fill, fill_for_review

__all__ = [
    "Attempt",
    "Darklight",
    "GenResult",
    "Outcome",
    "RenderError",
    "RenderFailed",
    "Rendered",
    "derive_seed",
    "render",
    "render_for_review",
]

log = structlog.get_logger(__name__)


class Outcome(StrEnum):
    OK = "ok"
    SAFETY_REJECTED = "safety_rejected"
    ERROR = "error"


@dataclass(frozen=True)
class GenResult:
    """What darklight returned for one request."""

    outcome: Outcome
    image: bytes | None = None
    detail: str = ""
    ref: str = ""  # the renderer's id for this attempt (darkroom: the record id), for the audit trail

    @classmethod
    def ok(cls, image: bytes, ref: str = "") -> GenResult:
        return cls(Outcome.OK, image=image, ref=ref)

    @classmethod
    def safety_rejected(cls, detail: str = "", ref: str = "") -> GenResult:
        return cls(Outcome.SAFETY_REJECTED, detail=detail, ref=ref)

    @classmethod
    def error(cls, detail: str, ref: str = "") -> GenResult:
        return cls(Outcome.ERROR, detail=detail, ref=ref)


class Darklight(Protocol):
    def generate(self, req: RenderRequest) -> GenResult: ...


@dataclass(frozen=True)
class Attempt:
    seed: int
    outcome: Outcome
    detail: str = ""
    ref: str = ""


@dataclass(frozen=True)
class Rendered:
    image: bytes
    request: RenderRequest  # the request that produced the image (its seed is the final seed)
    attempts: tuple[Attempt, ...] = field(default=())

    @property
    def seed(self) -> int:
        return self.request.seed

    @property
    def image_sha256(self) -> str:
        return hashlib.sha256(self.image).hexdigest()

    def provenance(self, *, asset_id: Bytes32 | bytes | str, consumer: str) -> Provenance:
        """The card provenance for this render (what the voucher's contentHash covers)."""
        return Provenance(
            template=self.request.template_id,
            template_version=self.request.template_version,
            template_hash=self.request.template_hash,
            seed=self.seed,
            attempts=len(self.attempts),
            asset_id=asset_id,
            consumer=consumer,
        )


class RenderError(Exception):
    """Rendering stopped; ``attempts`` holds every attempt made."""

    def __init__(self, message: str, attempts: tuple[Attempt, ...]):
        super().__init__(message)
        self.attempts = attempts


class RenderFailed(RenderError):
    """Every attempt up to ``max_attempts`` was rejected by the safety check."""


def derive_seed(seed: int, attempt: int) -> int:
    """Seed for attempt ``attempt`` (0-based). Attempt 0 uses ``seed`` itself.

    Later seeds are sha256-derived, so the whole sequence is reproducible from the first.
    """
    if attempt == 0:
        return seed
    digest = hashlib.sha256(f"argus-mint/seed/{seed}/{attempt}".encode()).digest()
    return int.from_bytes(digest[:4], "big") & SEED_MAX


def render(client: Darklight, t: Template, values: dict[str, str], seed: int, model: str | None = None) -> Rendered:
    """Render an approved template, retrying safety rejections with derived seeds.

    Raises :class:`RenderFailed` after ``t.max_attempts`` rejections, and :class:`RenderError`
    at once on a non-safety error (transport retries belong inside the client).
    """
    return _render(client, t, values, seed, fill, model)


def render_for_review(
    client: Darklight, t: Template, values: dict[str, str], seed: int, model: str | None = None
) -> Rendered:
    """Like :func:`render`, for a template of any status: sample images for its reviewer.

    The result must never be pinned, signed or minted (``argus-mint sample`` only writes
    local files). The same safety check and retries apply.
    """
    return _render(client, t, values, seed, fill_for_review, model)


def _render(
    client: Darklight, t: Template, values: dict[str, str], seed: int, fill_fn: Any, model: str | None = None
) -> Rendered:
    attempts: list[Attempt] = []
    for n in range(t.max_attempts):
        req = fill_fn(t, values, derive_seed(seed, n), model)
        result = client.generate(req)
        if result.outcome is Outcome.OK and not result.image:
            result = GenResult.error("darklight returned no image", ref=result.ref)
        attempts.append(Attempt(seed=req.seed, outcome=result.outcome, detail=result.detail, ref=result.ref))
        log.info(
            "render_attempt", template=t.id, version=t.version, attempt=n + 1, seed=req.seed, outcome=result.outcome
        )
        if result.outcome is Outcome.OK:
            assert result.image is not None
            return Rendered(image=result.image, request=req, attempts=tuple(attempts))
        if result.outcome is Outcome.ERROR:
            raise RenderError(f"darklight error on attempt {n + 1}: {result.detail}", tuple(attempts))
    raise RenderFailed(
        f"template {t.id} v{t.version}: safety check rejected all {t.max_attempts} attempts", tuple(attempts)
    )
