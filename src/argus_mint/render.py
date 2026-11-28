"""Render client: send a filled template to darklight, retry with a new seed when the
safety check rejects the image, and record every attempt.

Hard rule 3: retry, don't loosen. A rejection is retried with the next deterministic
seed, up to the template's ``max_attempts``; the prompt is never changed and the check
is never turned off. darklight's wire API is not settled yet, so the client is the
:class:`Darklight` protocol; the real HTTP client will implement it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import structlog

from argus_mint._codec import Bytes32, canonical
from argus_mint.metadata import Provenance
from argus_mint.templates import (
    SEED_MAX,
    InitImageRef,
    LoraChoice,
    RenderRequest,
    Template,
    fill,
    fill_for_review,
)

__all__ = [
    "Attempt",
    "Darklight",
    "GenResult",
    "InitImage",
    "Inpainter",
    "Outcome",
    "RenderError",
    "RenderFailed",
    "Rendered",
    "derive_seed",
    "render",
    "render_for_review",
    "render_requests",
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
    # The renderer's workflow, when it says: darkroom's {"name": "sdxl+lora1", "sha256": ...}.
    workflow: dict[str, str] | None = None
    version: str | None = None  # the renderer's own version, when it says (darkroom's record does)

    @classmethod
    def ok(
        cls, image: bytes, ref: str = "", workflow: dict[str, str] | None = None, version: str | None = None
    ) -> GenResult:
        return cls(Outcome.OK, image=image, ref=ref, workflow=workflow, version=version)

    @classmethod
    def safety_rejected(cls, detail: str = "", ref: str = "") -> GenResult:
        return cls(Outcome.SAFETY_REJECTED, detail=detail, ref=ref)

    @classmethod
    def error(cls, detail: str, ref: str = "") -> GenResult:
        return cls(Outcome.ERROR, detail=detail, ref=ref)


class Darklight(Protocol):
    def generate(self, req: RenderRequest) -> GenResult: ...


@dataclass(frozen=True)
class InitImage:
    """What an inpainting request carries beside it: the input image, its mask (white repaints),
    and the keypoints of the pose the figure is painted in, when the request names one."""

    image: bytes
    mask: bytes | None
    denoise: float
    # One person's 133 (x, y, score) points: the reviewed pose file's. None: no pose.
    pose: tuple[tuple[float, float, float], ...] | None = None

    def pose_sha256(self) -> str | None:
        """sha256 of the keypoints' canonical JSON (``RenderRequest.pose.keypoints_sha256``); None without a pose."""
        if self.pose is None:
            return None
        return hashlib.sha256(canonical([list(p) for p in self.pose])).hexdigest()

    def ref(self) -> InitImageRef:
        """What the request records about these bytes."""
        return InitImageRef(
            sha256=hashlib.sha256(self.image).hexdigest(),
            mask_sha256=hashlib.sha256(self.mask).hexdigest() if self.mask is not None else None,
            denoise=self.denoise,
        )

    def __repr__(self) -> str:  # never the pixels, in a log or a traceback
        return f"InitImage(<{len(self.image)} bytes>, mask={self.mask is not None}, denoise={self.denoise})"


class Inpainter(Protocol):
    """A renderer that also takes an input image and mask (darkroom's inpainting)."""

    def generate(self, req: RenderRequest, init: InitImage | None = None) -> GenResult: ...


@dataclass(frozen=True)
class Attempt:
    seed: int
    outcome: Outcome
    detail: str = ""
    ref: str = ""
    workflow: dict[str, str] | None = None  # the renderer's workflow (name, sha256), when it said

    def to_json(self) -> dict[str, Any]:
        """As drafts, library entries and publish output record it (``workflow`` only when known)."""
        out: dict[str, Any] = {"seed": self.seed, "outcome": str(self.outcome), "ref": self.ref, "detail": self.detail}
        if self.workflow:
            out["workflow"] = dict(self.workflow)
        return out


@dataclass(frozen=True)
class Rendered:
    image: bytes
    request: RenderRequest  # the request that produced the image (its seed is the final seed)
    attempts: tuple[Attempt, ...] = field(default=())
    renderer_version: str | None = None  # the version the renderer reported for the image, if any

    @property
    def seed(self) -> int:
        return self.request.seed

    @property
    def image_sha256(self) -> str:
        return hashlib.sha256(self.image).hexdigest()

    def provenance(self, *, asset_id: Bytes32 | bytes | str, consumer: str) -> Provenance:
        """The card provenance for this render (what the voucher's contentHash covers)."""
        return Provenance.for_render(self.request, attempts=len(self.attempts), asset_id=asset_id, consumer=consumer)


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


def render(
    client: Darklight,
    t: Template,
    values: dict[str, str],
    seed: int,
    model: str | None = None,
    loras: tuple[LoraChoice, ...] = (),
) -> Rendered:
    """Render an approved template, retrying safety rejections with derived seeds.

    Raises :class:`RenderFailed` after ``t.max_attempts`` rejections, and :class:`RenderError`
    at once on a non-safety error (transport retries belong inside the client).
    """
    return _render(client, t, values, seed, fill, model, loras)


def render_for_review(
    client: Darklight,
    t: Template,
    values: dict[str, str],
    seed: int,
    model: str | None = None,
    loras: tuple[LoraChoice, ...] = (),
) -> Rendered:
    """Like :func:`render`, for a template of any status: sample images for its reviewer.

    The result must never be pinned, signed or minted (``argus-mint sample`` only writes
    local files). The same safety check and retries apply.
    """
    return _render(client, t, values, seed, fill_for_review, model, loras)


def _render(
    client: Darklight,
    t: Template,
    values: dict[str, str],
    seed: int,
    fill_fn: Any,
    model: str | None = None,
    loras: tuple[LoraChoice, ...] = (),
) -> Rendered:
    try:
        return render_requests(
            client,
            lambda s: fill_fn(t, values, s, model, loras),
            seed,
            t.max_attempts,
            label=f"template {t.id} v{t.version}",
        )
    except RenderFailed as e:
        raise RenderFailed(
            f"template {t.id} v{t.version}: safety check rejected all {t.max_attempts} attempts", e.attempts
        ) from None


def render_requests(
    client: Darklight | Inpainter,
    build: Callable[[int], RenderRequest],
    seed: int,
    max_attempts: int,
    *,
    init: InitImage | None = None,
    label: str = "render",
) -> Rendered:
    """Render ``build(seed)``, retrying safety rejections with derived seeds (hard rule 3).

    ``build`` makes the request for a seed; only the seed differs between attempts. ``init``
    (inpainting) goes to the client with every attempt, unchanged. Raises :class:`RenderFailed`
    after ``max_attempts`` rejections and :class:`RenderError` at once on any other error.
    """
    attempts: list[Attempt] = []
    for n in range(max_attempts):
        req = build(derive_seed(seed, n))
        if init is not None and req.init_image != init.ref():
            raise ValueError("the request doesn't record the input image it is sent with")
        if (req.pose.keypoints_sha256 if req.pose else None) != (init.pose_sha256() if init else None):
            raise ValueError("the request doesn't record the pose it is sent with")
        result = client.generate(req) if init is None else client.generate(req, init=init)  # type: ignore[call-arg]
        if result.outcome is Outcome.OK and not result.image:
            result = GenResult.error("darklight returned no image", ref=result.ref)
        attempts.append(
            Attempt(
                seed=req.seed, outcome=result.outcome, detail=result.detail, ref=result.ref, workflow=result.workflow
            )
        )
        log.info("render_attempt", what=label, attempt=n + 1, seed=req.seed, outcome=result.outcome)
        if result.outcome is Outcome.OK:
            assert result.image is not None
            return Rendered(image=result.image, request=req, attempts=tuple(attempts), renderer_version=result.version)
        if result.outcome is Outcome.ERROR:
            raise RenderError(f"darklight error on attempt {n + 1}: {result.detail}", tuple(attempts))
    raise RenderFailed(f"{label}: safety check rejected all {max_attempts} attempts", tuple(attempts))
