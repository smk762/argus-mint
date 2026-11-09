"""Card frames: reviewed overlays composited onto a clean render before it is pinned.

A frame is a YAML file next to a PNG overlay (and optionally a font). The overlay is
transparent inside its art ``window``; the render is scaled to cover the window and drawn
underneath, then the overlay goes on top, then the card's name in the ``name`` slot, if the
frame has one. The name is metadata free text drawn onto the image, never sent to a model,
so hard rule 1 is untouched.

Frames are reviewed like templates (hard rule 2): ``draft | approved | retired``, and only
``approved`` frames go on a minted card (:func:`require_approved`). Drafts may be used by
``argus-mint sample`` for review. A frame's :meth:`LoadedFrame.hash` covers its YAML (minus
``status``) and the bytes of its overlay and font, and is recorded in the card's provenance.

Compositing needs Pillow (the ``frames`` extra). Apart from reading the frame's files this
module does no I/O.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from argus_mint._codec import canonical, keccak256
from argus_mint.metadata import FrameRef

__all__ = [
    "Frame",
    "FrameError",
    "LoadedFrame",
    "NameSlot",
    "Rect",
    "compose",
    "load_frame",
    "require_approved",
]

_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
_COLOUR_PATTERN = r"^#[0-9a-fA-F]{6}$"


class FrameError(ValueError):
    """A frame file is invalid, unusable for this card, or Pillow is missing."""


class Rect(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    def box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.width, self.y + self.height)


class NameSlot(Rect):
    """Where the card's name is drawn. The font shrinks until the name fits the slot."""

    font: str | None = None  # TTF/OTF path, relative to the frame file; Pillow's built-in font if unset
    size: int = Field(default=48, ge=6, le=512)
    min_size: int = Field(default=12, ge=6, le=512)
    colour: str = Field(default="#000000", pattern=_COLOUR_PATTERN)
    align: Literal["left", "center", "right"] = "center"


class Frame(BaseModel):
    """One version of a frame, as reviewed and recorded in its YAML file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=_ID_PATTERN)
    version: int = Field(ge=1)
    status: Literal["draft", "approved", "retired"]
    approved_by: str | None = None
    approved_at: date | None = None
    overlay: str = Field(min_length=1)  # PNG path, relative to the frame file
    window: Rect
    name: NameSlot | None = None

    @model_validator(mode="after")
    def _check(self) -> Frame:
        if self.status != "draft" and (not self.approved_by or self.approved_at is None):
            raise ValueError(f"status {self.status!r} requires approved_by and approved_at (approval is a human step)")
        return self


@dataclass(frozen=True)
class LoadedFrame:
    """A validated frame with its overlay (and font) bytes."""

    frame: Frame
    overlay: bytes
    font: bytes | None
    size: tuple[int, int]  # the overlay's (width, height): the finished card's size

    def hash(self) -> bytes:
        """keccak256 of the canonical frame (minus ``status``) plus its files' sha256."""
        doc: dict[str, Any] = {
            "frame": self.frame.model_dump(mode="json", exclude={"status"}),
            "overlay_sha256": hashlib.sha256(self.overlay).hexdigest(),
            "font_sha256": hashlib.sha256(self.font).hexdigest() if self.font is not None else None,
        }
        return keccak256(canonical(doc))

    def ref(self) -> FrameRef:
        """What a card's provenance records about this frame."""
        return FrameRef(id=self.frame.id, version=self.frame.version, hash=self.hash())


def _pil() -> Any:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as e:  # pragma: no cover - depends on the environment
        raise FrameError('frames need Pillow: pip install "argus-mint[frames]"') from e
    return Image, ImageDraw, ImageFont


def load_frame(path: Path) -> LoadedFrame:
    """Load and validate a frame YAML file and the files it names."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise FrameError(f"{path}: not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise FrameError(f"{path}: expected a mapping at the top level")
    try:
        frame = Frame.model_validate(raw)
    except ValidationError as e:
        raise FrameError(f"{path}: {e}") from e

    def read(rel: str) -> bytes:
        f = path.parent / rel
        try:
            return f.read_bytes()
        except OSError as e:
            raise FrameError(f"{path}: cannot read {rel}: {e}") from e

    overlay = read(frame.overlay)
    font = read(frame.name.font) if frame.name and frame.name.font else None
    image_mod, _, _ = _pil()
    try:
        with image_mod.open(io.BytesIO(overlay)) as im:
            if im.format != "PNG":
                raise FrameError(f"{path}: overlay must be a PNG, got {im.format}")
            size = im.size
    except OSError as e:
        raise FrameError(f"{path}: overlay is not a readable image: {e}") from e
    w, h = size
    for label, rect in (("window", frame.window), ("name", frame.name)):
        if rect is not None and (rect.x + rect.width > w or rect.y + rect.height > h):
            raise FrameError(f"{path}: {label} {rect.box()} lies outside the {w}x{h} overlay")
    return LoadedFrame(frame=frame, overlay=overlay, font=font, size=size)


def require_approved(f: LoadedFrame) -> LoadedFrame:
    """Return ``f`` if its frame is approved; a minted card may only use an approved frame."""
    if f.frame.status != "approved":
        raise FrameError(
            f"frame {f.frame.id} v{f.frame.version} is {f.frame.status}; only approved frames go on a card"
        )
    return f


def compose(f: LoadedFrame, art: bytes, name: str | None = None) -> bytes:
    """The finished card: ``art`` scaled to cover the window, the overlay on top, then ``name``.

    Deterministic for a given Pillow version. Returns PNG bytes without ancillary metadata.
    """
    image_mod, draw_mod, font_mod = _pil()
    slot = f.frame.name
    if slot is not None and not name:
        raise FrameError(f"frame {f.frame.id} v{f.frame.version} has a name slot: give the card's name")
    try:
        src = image_mod.open(io.BytesIO(art))
        src.load()
    except OSError as e:
        raise FrameError(f"the render is not a readable image: {e}") from e

    win = f.frame.window
    scale = max(win.width / src.width, win.height / src.height)
    scaled = src.convert("RGBA").resize(
        (max(win.width, round(src.width * scale)), max(win.height, round(src.height * scale))),
        image_mod.Resampling.LANCZOS,
    )
    left, top = (scaled.width - win.width) // 2, (scaled.height - win.height) // 2
    card = image_mod.new("RGBA", f.size, (0, 0, 0, 0))
    card.paste(scaled.crop((left, top, left + win.width, top + win.height)), (win.x, win.y))
    card = image_mod.alpha_composite(card, image_mod.open(io.BytesIO(f.overlay)).convert("RGBA"))

    if slot is not None and name:
        draw = draw_mod.Draw(card)
        font, bbox = _fit(font_mod, draw, f.font, name, slot)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = {"left": slot.x, "center": slot.x + (slot.width - text_w) // 2, "right": slot.x + slot.width - text_w}[
            slot.align
        ]
        y = slot.y + (slot.height - text_h) // 2
        draw.text((x - bbox[0], y - bbox[1]), name, font=font, fill=slot.colour)

    out = io.BytesIO()
    card.save(out, format="PNG", optimize=False, compress_level=6)
    return out.getvalue()


def _fit(font_mod: Any, draw: Any, font_bytes: bytes | None, text: str, slot: NameSlot) -> tuple[Any, tuple]:
    """The largest font size from ``slot.size`` down to ``slot.min_size`` at which ``text`` fits."""
    for size in range(slot.size, slot.min_size - 1, -1):
        font = font_mod.truetype(io.BytesIO(font_bytes), size) if font_bytes else font_mod.load_default(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= slot.width and bbox[3] - bbox[1] <= slot.height:
            return font, bbox
    raise FrameError(f"name {text!r} does not fit the frame's name slot even at size {slot.min_size}")
