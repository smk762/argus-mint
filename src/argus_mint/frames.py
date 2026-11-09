"""Card frames: reviewed overlays composited onto a clean render before it is pinned.

A frame is a YAML file next to a PNG overlay (and optionally fonts). The overlay is
transparent inside its art ``window``. The render is scaled to cover the window and drawn
underneath, the overlay goes on top, then each text slot is filled: the card's name, its
bio, and whatever display text the consumer supplies (a class line, stats...). Text is
wrapped and shrunk to fit its slot, and clipped with an ellipsis if it still doesn't fit at
the slot's smallest size. Card text is drawn onto the image; none of it is sent to the image
model, so hard rule 1 is untouched.

Frames are reviewed like templates (hard rule 2): ``draft | approved | retired``, and only
``approved`` frames go on a minted card (:func:`require_approved`). Drafts may be used by
``argus-mint sample`` for review. A frame's :meth:`LoadedFrame.hash` covers its YAML (minus
``status``) and the bytes of its overlay and fonts, and is recorded in the card's provenance.

Compositing needs Pillow (the ``frames`` extra). Apart from reading the frame's files this
module does no I/O.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
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
    "Rect",
    "TextSlot",
    "compose",
    "fit_text",
    "load_frame",
    "require_approved",
]

_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
_SLOT_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
_COLOUR_PATTERN = r"^#[0-9a-fA-F]{6}$"
ELLIPSIS = "…"


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


class TextSlot(Rect):
    """A box for one piece of card text.

    The text is drawn at ``size`` if it fits, else at the largest size down to ``min_size``
    that fits; with ``wrap`` it breaks across lines. If it doesn't fit even at ``min_size``,
    it is clipped and ends in an ellipsis.
    """

    font: str | None = None  # TTF/OTF path, relative to the frame file; Pillow's built-in font if unset
    size: int = Field(default=48, ge=6, le=512)
    min_size: int = Field(default=12, ge=6, le=512)
    colour: str = Field(default="#000000", pattern=_COLOUR_PATTERN)
    align: Literal["left", "center", "right"] = "center"
    valign: Literal["top", "middle"] = "middle"
    wrap: bool = False
    line_spacing: float = Field(default=1.15, ge=0.8, le=3.0)

    @model_validator(mode="after")
    def _sizes(self) -> TextSlot:
        if self.min_size > self.size:
            raise ValueError("min_size is larger than size")
        return self


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
    texts: dict[str, TextSlot] = Field(
        default_factory=dict
    )  # slot name -> box; "name" and "bio" are filled by argus-mint

    @model_validator(mode="after")
    def _check(self) -> Frame:
        import re

        for slot in self.texts:
            if not re.match(_SLOT_PATTERN, slot):
                raise ValueError(f"text slot name {slot!r} must match {_SLOT_PATTERN}")
        if self.status != "draft" and (not self.approved_by or self.approved_at is None):
            raise ValueError(f"status {self.status!r} requires approved_by and approved_at (approval is a human step)")
        return self


@dataclass(frozen=True)
class LoadedFrame:
    """A validated frame with its overlay and font bytes."""

    frame: Frame
    overlay: bytes
    size: tuple[int, int]  # the overlay's (width, height): the finished card's size
    fonts: dict[str, bytes] = field(default_factory=dict)  # by the path the frame names them with

    def hash(self) -> bytes:
        """keccak256 of the canonical frame (minus ``status``) plus its files' sha256."""
        doc: dict[str, Any] = {
            "frame": self.frame.model_dump(mode="json", exclude={"status"}),
            "overlay_sha256": hashlib.sha256(self.overlay).hexdigest(),
            "fonts_sha256": {p: hashlib.sha256(b).hexdigest() for p, b in sorted(self.fonts.items())},
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
    fonts = {s.font: read(s.font) for s in frame.texts.values() if s.font}
    image_mod, _, font_mod = _pil()
    try:
        with image_mod.open(io.BytesIO(overlay)) as im:
            if im.format != "PNG":
                raise FrameError(f"{path}: overlay must be a PNG, got {im.format}")
            size = im.size
    except OSError as e:
        raise FrameError(f"{path}: overlay is not a readable image: {e}") from e
    for rel, data in fonts.items():
        try:
            font_mod.truetype(io.BytesIO(data), 12)
        except OSError as e:
            raise FrameError(f"{path}: {rel} is not a usable font: {e}") from e
    w, h = size
    for label, rect in (("window", frame.window), *frame.texts.items()):
        if rect.x + rect.width > w or rect.y + rect.height > h:
            raise FrameError(f"{path}: {label} {rect.box()} lies outside the {w}x{h} overlay")
    return LoadedFrame(frame=frame, overlay=overlay, size=size, fonts=fonts)


def require_approved(f: LoadedFrame) -> LoadedFrame:
    """Return ``f`` if its frame is approved; a minted card may only use an approved frame."""
    if f.frame.status != "approved":
        raise FrameError(
            f"frame {f.frame.id} v{f.frame.version} is {f.frame.status}; only approved frames go on a card"
        )
    return f


def compose(f: LoadedFrame, art: bytes, texts: dict[str, str] | None = None) -> bytes:
    """The finished card: ``art`` scaled to cover the window, the overlay on top, then ``texts``.

    ``texts`` maps the frame's slot names to the text drawn in them; a slot without text is
    left empty, and text for a slot the frame doesn't have is an error. Deterministic for a
    given Pillow version. Returns PNG bytes without ancillary metadata.
    """
    texts = {k: v for k, v in (texts or {}).items() if v}
    unknown = texts.keys() - f.frame.texts.keys()
    if unknown:
        raise FrameError(
            f"frame {f.frame.id} v{f.frame.version} has no text slot {sorted(unknown)}; "
            f"its slots: {sorted(f.frame.texts)}"
        )
    image_mod, draw_mod, font_mod = _pil()
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

    draw = draw_mod.Draw(card)
    for slot_name, text in sorted(texts.items()):
        slot = f.frame.texts[slot_name]
        font_bytes = f.fonts.get(slot.font) if slot.font else None

        def load(size: int, font_bytes: bytes | None = font_bytes) -> Any:
            return font_mod.truetype(io.BytesIO(font_bytes), size) if font_bytes else font_mod.load_default(size)

        font, lines = fit_text(load, text, slot)
        ascent, descent = font.getmetrics()
        line_h = round((ascent + descent) * slot.line_spacing)
        block_h = line_h * (len(lines) - 1) + ascent + descent
        y = slot.y if slot.valign == "top" else slot.y + (slot.height - block_h) // 2
        for line in lines:
            width = font.getlength(line)
            x = {"left": slot.x, "center": slot.x + (slot.width - width) / 2, "right": slot.x + slot.width - width}[
                slot.align
            ]
            draw.text((round(x), y), line, font=font, fill=slot.colour, anchor="la")
            y += line_h

    out = io.BytesIO()
    card.save(out, format="PNG", optimize=False, compress_level=6)
    return out.getvalue()


def fit_text(load: Any, text: str, slot: TextSlot) -> tuple[Any, list[str]]:
    """The font and lines for ``text`` in ``slot``: the largest size that fits, else clipped at ``min_size``.

    ``load(size)`` returns a font at that size.
    """
    text = " ".join(text.split())
    for size in range(slot.size, slot.min_size - 1, -1):
        font = load(size)
        lines = _wrap(font, text, slot.width) if slot.wrap else [text]
        if _fits(font, lines, slot):
            return font, lines
    font = load(slot.min_size)
    lines = _wrap(font, text, slot.width) if slot.wrap else [text]
    ascent, descent = font.getmetrics()
    line_h = round((ascent + descent) * slot.line_spacing)
    max_lines = max(1, 1 + (slot.height - ascent - descent) // line_h) if slot.wrap else 1
    kept = lines[:max_lines]
    kept[-1] = _ellipsize(font, " ".join([kept[-1], *lines[max_lines:]]), slot.width)
    return font, kept


def _fits(font: Any, lines: list[str], slot: TextSlot) -> bool:
    ascent, descent = font.getmetrics()
    line_h = round((ascent + descent) * slot.line_spacing)
    height = line_h * (len(lines) - 1) + ascent + descent
    return height <= slot.height and all(font.getlength(line) <= slot.width for line in lines)


def _wrap(font: Any, text: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if current and font.getlength(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _ellipsize(font: Any, text: str, width: int) -> str:
    """``text`` cut back to fit ``width`` with a trailing ellipsis (at a word boundary when possible)."""
    if font.getlength(text) <= width:
        return text
    words = text.split(" ")
    while len(words) > 1 and font.getlength(" ".join(words) + ELLIPSIS) > width:
        words.pop()
    clipped = " ".join(words).rstrip(",;:—-")
    while clipped and font.getlength(clipped + ELLIPSIS) > width:
        clipped = clipped[:-1]
    return clipped + ELLIPSIS
