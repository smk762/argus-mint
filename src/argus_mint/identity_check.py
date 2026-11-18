"""The identity check on a teaser (#51): does the figure painted in look like the identity it's meant to be?

After a teaser of an **embodied** identity is composed, argus-proof's ``POST /score`` scores its
unframed art (the draft's ``art.png``), cropped to the scene's ``character_area``, against the
held-out references of the identity's ``proof_export``, on its ``check_metric`` (``identity``:
ArcFace, a face; ``likeness``: DINOv2, anything else). The result goes in the draft's
``auto_check.identity`` (:class:`~argus_mint.cards.IdentityCheck`) with proof's provenance: its
wire version and the scorer's pinned weights.

- At least the identity's ``min_score``: ``passed``.
- Under it: ``below``, and the draft is held for a person with the reason in words.
- No ``min_score`` yet: ``uncalibrated``. The score is recorded and nothing is held on it (the
  pilot sets the threshold from these scores), but it can't stand in for the moderator either.
- A viewpoint identity (nobody painted in) or an embodied one with no ``proof_export`` yet:
  ``skipped``, saying why. An embodied skip holds the draft.
- proof unreachable, answering 503 or not configured: ``unavailable``; any other refusal or an
  answer that can't be read: ``error``. Both hold the draft: a teaser is never let through
  unchecked. The moderator retries with ``POST /drafts/{id}/identity-check``.

The check never decides on its own: it is one of auto-moderation's checks
(``moderation.auto_check``), which records the moderator's approval only when auto-moderation is
on and every check passed. The owner always reviews (hard rule 9).

**The crop's coordinates.** ``character_area`` is in the scene's render pixels
(``scene.width`` x ``scene.height``): the background is rendered at that size, the inpainting
mask is drawn at that size over the same area (``teasers.mask_png``), and darkroom returns the
inpainted art at its input's size. So the area is the crop in the art's own pixels, which is what
proof's ``CropBox`` takes. That is checked rather than assumed: art of another size, or with an
EXIF orientation (proof crops after applying it), is an ``error``, and proof's echo of the crop
must be the crop sent.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any, Protocol

from argus_mint.cards import IdentityCheck, ScoreBox, ScorerFile, sha256_hex
from argus_mint.projects import Identity, Scene
from argus_mint.training_clients import TrainingClientError

__all__ = ["PROOF_MAJOR", "TARGET_CATEGORY", "Scorer", "check_identity"]

PROOF_MAJOR = "1"  # proof's wire major mint reads a ScoreResult of
TARGET_CATEGORY = "identity"  # proof's category for a subject's look (its ArcFace scorer only measures this one)
_EXIF_ORIENTATION = 0x0112


class Scorer(Protocol):
    """argus-proof's ``POST /score`` (``training_clients.ProofClient``). Raises
    ``TrainingClientError`` (``status`` None when proof didn't answer)."""

    def score(self, images: list[tuple[str, bytes]], request: dict[str, Any]) -> dict[str, Any]: ...


def check_identity(
    *, art: bytes, identity: Identity, scene: Scene, proof: Scorer | None, now: datetime | None = None
) -> IdentityCheck:
    """Score ``art`` (a teaser's unframed art, painted into ``scene``) for ``identity``. Never raises
    for proof's sake: what went wrong is the record's ``outcome`` and ``reason``."""
    who = f"identity {identity.id} v{identity.version}"
    base: dict[str, Any] = {
        "at": now or datetime.now(UTC),
        "art_sha256": sha256_hex(art),
        "presence": identity.presence,
        "metric": identity.check_metric,
        "min_score": identity.min_score,
        "export": identity.proof_export,
    }

    def record(outcome: str, reason: str, **more: Any) -> IdentityCheck:
        return IdentityCheck.model_validate(base | more | {"outcome": outcome, "reason": reason})

    if identity.presence == "viewpoint":
        return record("skipped", f"{who} is a viewpoint: nobody is painted in, so there is no figure to score")
    if identity.proof_export is None:
        return record(
            "skipped", f"{who} has no proof_export yet (no LoRA of it has passed proof), so nothing to score against"
        )
    if identity.check_metric is None:
        return record("skipped", f"{who} names no check_metric, so proof isn't asked")
    area = scene.character_area
    if area is None:
        return record("error", f"scene {scene.id} v{scene.version} has no character_area to score")
    crop = ScoreBox(x=area.x, y=area.y, width=area.width, height=area.height)
    base["crop"] = crop
    found = _pixels(art)
    if isinstance(found, str):
        return record("error", found)
    if found != (scene.width, scene.height):
        return record(
            "error",
            f"the art is {found[0]}x{found[1]}, not scene {scene.id}'s {scene.width}x{scene.height}, "
            "so its character_area doesn't name the figure's pixels",
        )
    if proof is None:
        return record("unavailable", "no argus-proof is configured (ARGUS_MINT_PROOF_URL), so the figure wasn't scored")

    metric = identity.check_metric
    request = {
        "export": identity.proof_export,
        "scorers": [metric],
        "target_category": TARGET_CATEGORY,
        "crops": [crop.model_dump()],
    }
    try:
        result = proof.score([("art.png", art)], request)
    except TrainingClientError as e:
        if e.status is None or e.status == 503:
            return record("unavailable", f"proof couldn't score it ({e}); the moderator can retry the check")
        return record("error", f"proof refused to score it ({e})")
    return _judge(result, metric, crop, identity, record)


def _pixels(art: bytes) -> tuple[int, int] | str:
    """The art's size, or why its pixels can't be taken as they are."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(art)) as im:
            if im.getexif().get(_EXIF_ORIENTATION, 1) not in (1, None):
                return "the art carries an EXIF orientation, so proof would crop other pixels than the area"
            return im.size
    except OSError as e:
        return f"the art can't be read as an image: {e}"


def _judge(result: Any, metric: str, crop: ScoreBox, identity: Identity, record: Any) -> IdentityCheck:
    if not isinstance(result, dict):
        return record("error", "proof's answer isn't a ScoreResult")
    version = result.get("proof_version")
    if not isinstance(version, str) or version.split(".")[0] != PROOF_MAJOR:
        return record("error", f"proof answered wire {version!r}; mint reads {PROOF_MAJOR}.x")
    more: dict[str, Any] = {"proof_version": version}
    for row in result.get("scorers") or []:
        if isinstance(row, dict) and row.get("metric") == metric:
            more["scorer"] = row.get("name") if isinstance(row.get("name"), str) else None
            more["scorer_version"] = row.get("version") if isinstance(row.get("version"), str) else None
            mf = row.get("model_file")
            if isinstance(mf, dict):
                try:
                    more["model_file"] = ScorerFile.model_validate(mf)
                except ValueError:
                    return record("error", f"proof's {metric} scorer names weights that aren't a pinned file", **more)
            break
    images = result.get("images")
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
        return record("error", "proof's answer doesn't score the one image sent", **more)
    echoed = images[0].get("crop")
    if echoed != crop.model_dump():
        return record("error", f"proof scored crop {echoed!r}, not the character_area sent", **more)
    score = (images[0].get("metrics") or {}).get(metric)
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        why = "no face found" if metric == "identity" else "its scorer didn't run"
        return record("error", f"proof returned no {metric} score ({why}, or no references)", **more)
    score = float(score)
    more["score"] = score
    who = f"identity {identity.id} v{identity.version}"
    if identity.min_score is None:
        return record(
            "uncalibrated",
            f"{metric} {score:.3f} recorded; {who} has no min_score yet (uncalibrated), so it isn't judged",
            **more,
        )
    if score >= identity.min_score:
        return record("passed", f"{metric} {score:.3f} is at least min_score {identity.min_score}", **more)
    return record("below", f"{metric} {score:.3f} is under {who}'s min_score {identity.min_score}", **more)
