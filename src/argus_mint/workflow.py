"""The card workflow shared by the CLI and the HTTP server: create a draft, publish it.

Reviewing sits between the two (:func:`argus_mint.cards.review`); :func:`publish` runs
:func:`argus_mint.cards.check_publishable` first, so nothing is pinned or signed before
the moderator and the owner have both approved the exact image (hard rule 9).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from eth_account.signers.local import LocalAccount

from argus_mint._codec import hex0x
from argus_mint.bios import Bio, BioError, BioFailed, BioTemplate, TextGuard, Writer, write_bio
from argus_mint.cards import BioRecord, CardDraft, check_publishable, sha256_hex
from argus_mint.frames import FrameError, LoadedFrame, compose, require_approved
from argus_mint.metadata import card_metadata, content_hash
from argus_mint.pin import Pinner
from argus_mint.render import Darklight, RenderError, render, render_for_review
from argus_mint.templates import Template, fill
from argus_mint.vouchers import Domain, MintVoucher, sign, typed_data

__all__ = [
    "RESERVED_SLOTS",
    "Made",
    "Sample",
    "card_texts",
    "check_bio",
    "check_texts",
    "create_draft",
    "publish",
    "sample_card",
]

RESERVED_SLOTS = ("name", "bio")  # filled by argus-mint, never by the consumer


class Made:
    """A new draft with its files, ready for :func:`argus_mint.cards.save_draft`."""

    def __init__(self, draft: CardDraft, art: bytes, image: bytes, bio: Bio | None):
        self.draft, self.art, self.image, self.bio = draft, art, image, bio


def check_texts(texts: dict[str, str], frame: LoadedFrame | None) -> None:
    """Consumer display text must name real, non-reserved slots of the frame."""
    if not texts:
        return
    if frame is None:
        raise FrameError("display text needs a frame to draw it on")
    reserved = sorted(set(texts) & set(RESERVED_SLOTS))
    if reserved:
        raise FrameError(f"the {', '.join(reserved)} slot is filled by argus-mint")
    unknown = sorted(set(texts) - set(frame.frame.texts))
    if unknown:
        raise FrameError(
            f"frame {frame.frame.id} v{frame.frame.version} has no text slot {unknown}; "
            f"its slots: {sorted(frame.frame.texts)}"
        )


def check_bio(bio: BioTemplate, values: dict[str, str], template: Template) -> None:
    """Raise ``BioError`` if ``bio`` can't write this card's bio: it isn't approved, or doesn't fit the template.

    Run before rendering, so a bio template that would be refused doesn't cost a render first.
    """
    if bio.status != "approved":
        raise BioError(f"bio {bio.id} v{bio.version} is {bio.status}; only approved bio templates go on a card")
    bio.fill(values, template)


def card_texts(frame: LoadedFrame, name: str, bio: str | None, extra: dict[str, str]) -> dict[str, str]:
    """What to draw in each slot: the name and bio where the frame has those slots, plus display text."""
    slots = frame.frame.texts
    return {k: v for k, v in (("name", name), ("bio", bio)) if v and k in slots} | extra


def create_draft(
    *,
    template: Template,
    values: dict[str, str],
    seed: int,
    asset_id: bytes,
    to: str,
    name: str,
    renderer: Darklight | None = None,
    renderer_name: str = "darkroom",
    image: bytes | None = None,
    image_ref: str = "",
    description: str = "",
    external_url: str | None = None,
    consumer: str = "argus-mint",
    frame: LoadedFrame | None = None,
    bio: BioTemplate | None = None,
    writer: Writer | None = None,
    guard: TextGuard | None = None,
    texts: dict[str, str] | None = None,
) -> Made:
    """Render (or take ``image``), write the bio, frame the card. Nothing is pinned or signed.

    Raises the domain errors of each step: ``TemplateError``, ``RenderError``, ``DarkroomError``,
    ``BioError``/``BioFailed``, ``FrameError``.
    """
    texts = dict(texts or {})
    if frame is not None:
        require_approved(frame)
    check_texts(texts, frame)
    if (image is None) == (renderer is None):
        raise ValueError("give exactly one of renderer or image")
    if bio is not None and (writer is None or guard is None):
        raise ValueError("a bio needs a writer and a text guard")
    if bio is not None:
        check_bio(bio, values, template)
    if image is not None:
        req = fill(template, values, seed)
        art, attempts = image, [{"seed": req.seed, "outcome": "supplied", "ref": image_ref}]
        renderer_name = "supplied"
    else:
        assert renderer is not None
        rendered = render(renderer, template, values, seed)
        art, req = rendered.image, rendered.request
        attempts = _render_attempts(rendered.attempts)
    b = write_bio(writer, guard, bio, values, template, name) if bio and writer and guard else None
    card_image = compose(frame, art, card_texts(frame, name, b.text if b else None, texts)) if frame else art
    draft = CardDraft(
        request=req,
        attempts=tuple(attempts),
        renderer=renderer_name,
        asset_id=asset_id,
        to=to,
        name=name,
        description=description or (b.text if b else ""),
        external_url=external_url,
        consumer=consumer,
        frame=frame.ref() if frame else None,
        bio=BioRecord(ref=b.template.ref(), text=b.text, attempts=tuple(bio_attempts(b.attempts))) if b else None,
        texts=texts,
        art_sha256=sha256_hex(art),
        image_sha256=sha256_hex(card_image),
        created_at=datetime.now(UTC),
    )
    return Made(draft, art, card_image, b)


def bio_attempts(attempts: Any) -> list[dict[str, Any]]:
    return [{"outcome": a.outcome, "detail": a.detail, "text": a.text} for a in attempts]


def publish(
    draft: CardDraft,
    art: bytes,
    image: bytes,
    *,
    pinner: Pinner,
    signer: LocalAccount,
    chain_id: int,
    card: str,
    price: int = 0,
    deadline: int | None = None,
    ttl: int = 3600,
) -> dict[str, Any]:
    """Pin an approved draft, build its metadata and sign the voucher. Raises ``DraftError``/``NotApproved``
    (before anything leaves the machine) or ``PinError``."""
    check_publishable(draft, art, image)
    req = draft.request
    stem = f"{req.template_id}-v{req.template_version}-{req.seed}"
    art_cid = pinner.pin_bytes(art, f"{stem}-art.png") if draft.frame is not None else None
    image_cid = pinner.pin_bytes(image, f"{stem}.png")
    doc = card_metadata(
        name=draft.name,
        description=draft.description,
        image_cid=image_cid,
        external_url=draft.external_url,
        attributes=[],
        provenance=draft.provenance(art_cid),
    )
    metadata_cid = pinner.pin_json(doc, f"{req.template_id}-metadata.json")
    voucher = MintVoucher(
        to=draft.to,
        asset_id=draft.asset_id,
        content_hash=content_hash(doc),
        price=price,
        deadline=deadline if deadline is not None else int(time.time()) + ttl,
    )
    domain = Domain(chain_id=chain_id, verifying_contract=card)
    return {
        "signer": signer.address,
        "render": {
            "renderer": draft.renderer,
            "prompt": req.prompt,
            "seed": req.seed,
            "attempts": list(draft.attempts),
            "art_cid": art_cid,
            "image_cid": image_cid,
        },
        "reviews": [r.model_dump(mode="json") for r in draft.reviews],
        "metadata": doc,
        "metadata_cid": metadata_cid,
        "voucher": voucher.model_dump(mode="json"),
        "signature": hex0x(sign(voucher, domain, signer)),
        "typed_data": typed_data(voucher, domain),
    }


class Sample:
    """One sample card for a reviewer: never pinned, signed or minted (hard rule 2)."""

    def __init__(self, item: dict[str, Any], art: bytes | None, framed: bytes | None):
        self.item, self.art, self.framed = item, art, framed


def sample_card(
    *,
    renderer: Darklight,
    template: Template,
    values: dict[str, str],
    seed: int,
    frame: LoadedFrame | None = None,
    bio: BioTemplate | None = None,
    writer: Writer | None = None,
    guard: TextGuard | None = None,
    name: str = "Sample",
    texts: dict[str, str] | None = None,
) -> Sample:
    """Render a sample of a template (any status), with an optional bio and frame (any status).

    A failed render or bio is reported in the item (``error``), not raised, so a batch of
    samples keeps going; a backend failure (``DarkroomError``, ``BioError``) is raised.
    """
    texts = dict(texts or {})
    check_texts(texts, frame)
    item: dict[str, Any] = {"variables": dict(sorted(values.items()))}
    try:
        r = render_for_review(renderer, template, values, seed)
    except RenderError as e:
        item |= {"error": str(e), "attempts": _render_attempts(e.attempts)}
        return Sample(item, None, None)
    item |= {"prompt": r.request.prompt, "seed": r.seed, "attempts": _render_attempts(r.attempts)}
    bio_text = None
    if bio is not None and writer is not None and guard is not None:
        try:
            b = write_bio(writer, guard, bio, values, template, name, review=True)
        except BioFailed as e:
            item |= {"bio_error": str(e), "bio_attempts": bio_attempts(e.attempts)}
        else:
            bio_text = b.text
            item |= {"bio": b.text, "bio_attempts": bio_attempts(b.attempts)}
    framed = compose(frame, r.image, card_texts(frame, name, bio_text, texts)) if frame else None
    return Sample(item, r.image, framed)


def _render_attempts(attempts: Any) -> list[dict[str, Any]]:
    return [{"seed": a.seed, "outcome": str(a.outcome), "ref": a.ref, "detail": a.detail} for a in attempts]
