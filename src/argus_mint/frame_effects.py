"""Frame effects: reviewed, deterministic changes to the art's own pixels, inside the frame's window.

An overlay can only add pixels on top of the art. A viewpoint frame (a camera or feed look) also
needs to change the art itself: a lens's bulge, scanlines, signal noise, colour fringing, dark
corners. A frame lists them in ``effects``; :func:`apply_effects` runs them in that order on the
art once it is scaled and cropped to cover the window, before the overlay and text go on
(``frames.compose``). Nothing outside the window is touched.

Each effect has fixed parameters, validated with bounds, and is part of the frame's YAML, so it
is reviewed with the frame and covered by its hash. A frame without effects keeps the hash it
had before effects existed.

**Determinism (hard rule 7).** The same art and frame give the same bytes. Only Pillow does the
pixel work (no numpy: nothing here needs an array library, and Pillow already decides the bytes
of every composited card), with integer lookup tables and fixed resampling. The noise isn't
from an RNG: its bytes are SHAKE-256 of a domain tag, the art's sha256 and the effect's place in
the list, a function fixed by its standard. It is seeded by the **art's sha256** rather than the
render's seed: every compose has the art in hand (a draft, its owner's edit redrawn, a frame
preview on a sample's art, a teaser's bake), so they all agree byte for byte without passing a
seed around; the art is pinned in the token's provenance, so anyone can recompute the noise;
and two teasers on the same background with the same frame get the same grain, as they should.

Needs Pillow (the ``frames`` extra); no I/O.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "MAX_EFFECTS",
    "Barrel",
    "ChromaticAberration",
    "Effect",
    "Noise",
    "Scanlines",
    "Vignette",
    "apply_effects",
    "check_window",
]

MAX_EFFECTS = 8
MESH = 32  # the barrel distortion's grid: MESH x MESH cells, each mapped exactly at its corners
NOISE_TAG = b"argus-mint/frame-effects/noise/v1\x00"


class _Effect(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Barrel(_Effect):
    """A lens's bulge: the centre magnified, the window's corners kept where they are.

    A pixel at distance ``r`` from the centre (1 at a corner) shows the art at
    ``r * (1 + strength * r^2) / (1 + strength)``. 0.1 is mild, 0.3 strong.
    """

    type: Literal["barrel"]
    strength: float = Field(gt=0, le=0.5)


class Scanlines(_Effect):
    """Dark horizontal lines: ``thickness`` rows in every ``spacing``, darkened by ``opacity``."""

    type: Literal["scanlines"]
    spacing: int = Field(ge=2, le=32)
    thickness: int = Field(ge=1, le=16)
    opacity: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _gap(self) -> Scanlines:
        if self.thickness >= self.spacing:
            raise ValueError("scanlines need thickness < spacing (a gap between lines)")
        return self


class Noise(_Effect):
    """Signal noise: each pixel moves by up to ``amount`` of full scale, up or down, evenly spread.
    ``monochrome``: the same change in each channel (grain); else each channel its own (colour speckle)."""

    type: Literal["noise"]
    amount: float = Field(gt=0, le=0.5)
    monochrome: bool


class ChromaticAberration(_Effect):
    """Colour fringing: red spreads out from the centre and blue pulls in, by ``shift`` pixels at the edges."""

    type: Literal["chromatic_aberration"]
    shift: int = Field(ge=1, le=16)


class Vignette(_Effect):
    """Dark corners: from ``start`` (a fraction of the centre-to-corner distance) the art darkens
    smoothly, to ``strength`` at the corners."""

    type: Literal["vignette"]
    strength: float = Field(gt=0, le=1)
    start: float = Field(ge=0, lt=1)


Effect = Annotated[Barrel | Scanlines | Noise | ChromaticAberration | Vignette, Field(discriminator="type")]


def check_window(effects: tuple[Any, ...], width: int, height: int) -> list[str]:
    """What makes ``effects`` unusable in a window this size."""
    out = []
    for n, e in enumerate(effects, 1):
        if isinstance(e, ChromaticAberration) and 4 * e.shift >= min(width, height):
            out.append(f"effect {n} (chromatic_aberration): shift {e.shift} is too large for a {width}x{height} window")
    return out


def _pil() -> Any:
    from PIL import Image, ImageChops, ImageDraw

    return Image, ImageChops, ImageDraw


def apply_effects(window_art: Any, effects: tuple[Any, ...], art_sha256: bytes) -> Any:
    """``window_art`` (an RGBA Pillow image, the art covering the window) with ``effects`` applied
    in order. ``art_sha256`` (32 bytes) seeds the noise. The alpha channel is kept as it is."""
    im = window_art
    for n, e in enumerate(effects):
        if isinstance(e, Barrel):
            im = _barrel(im, e)
        elif isinstance(e, Scanlines):
            im = _scanlines(im, e)
        elif isinstance(e, Noise):
            im = _noise(im, e, art_sha256, n)
        elif isinstance(e, ChromaticAberration):
            im = _chromatic(im, e)
        elif isinstance(e, Vignette):
            im = _vignette(im, e)
        else:  # pragma: no cover - the model admits no other
            raise TypeError(f"unknown effect {e!r}")
    return im


def _barrel(im: Any, e: Barrel) -> Any:
    image_mod, _, _ = _pil()
    w, h = im.size
    cx, cy = w / 2, h / 2
    r2max = cx * cx + cy * cy
    k = e.strength

    def src(x: float, y: float) -> tuple[float, float]:
        dx, dy = x - cx, y - cy
        f = (1 + k * (dx * dx + dy * dy) / r2max) / (1 + k)
        return cx + dx * f, cy + dy * f

    xs = [round(w * i / MESH) for i in range(MESH + 1)]
    ys = [round(h * j / MESH) for j in range(MESH + 1)]
    mesh = []
    for j in range(MESH):
        for i in range(MESH):
            x0, x1, y0, y1 = xs[i], xs[i + 1], ys[j], ys[j + 1]
            if x1 == x0 or y1 == y0:
                continue
            quad = (*src(x0, y0), *src(x0, y1), *src(x1, y1), *src(x1, y0))
            mesh.append(((x0, y0, x1, y1), quad))
    return im.transform((w, h), image_mod.Transform.MESH, mesh, resample=image_mod.Resampling.BICUBIC)


def _scanlines(im: Any, e: Scanlines) -> Any:
    image_mod, _, draw_mod = _pil()
    w, h = im.size
    mask = image_mod.new("L", (w, h), 0)
    d = draw_mod.Draw(mask)
    level = round(e.opacity * 255)
    for y in range(0, h, e.spacing):
        d.rectangle((0, y, w - 1, min(h, y + e.thickness) - 1), fill=level)
    return _darken(im, mask)


def _darken(im: Any, mask: Any) -> Any:
    """``im`` blended toward black by ``mask`` (0 untouched, 255 black); alpha kept."""
    image_mod, _, _ = _pil()
    alpha = im.getchannel("A")
    out = image_mod.composite(image_mod.new("RGBA", im.size, (0, 0, 0, 255)), im, mask)
    out.putalpha(alpha)
    return out


def _noise_bytes(art_sha256: bytes, index: int, channel: int, count: int) -> bytes:
    return hashlib.shake_256(NOISE_TAG + art_sha256 + bytes([index, channel])).digest(count)


def _noise(im: Any, e: Noise, art_sha256: bytes, index: int) -> Any:
    image_mod, chops, _ = _pil()
    w, h = im.size
    peak = e.amount * 255
    up = [max(0, round((v - 128) / 128 * peak)) for v in range(256)]  # 129..255 lift, 0..127 lower
    down = [max(0, round((128 - v) / 128 * peak)) for v in range(256)]
    r, g, b, a = im.split()
    out = []
    for c, band in enumerate((r, g, b)):
        if c == 0 or not e.monochrome:
            n = image_mod.frombytes("L", (w, h), _noise_bytes(art_sha256, index, 0 if e.monochrome else c, w * h))
            lift, lower = n.point(up), n.point(down)
        out.append(chops.subtract(chops.add(band, lift), lower))
    return image_mod.merge("RGBA", (*out, a))


def _chromatic(im: Any, e: ChromaticAberration) -> Any:
    image_mod, _, _ = _pil()
    w, h = im.size
    # check_window refuses a shift this window can't take; clamped here too, so a smaller image never crashes.
    s = min(e.shift, (min(w, h) - 1) // 2)
    if s < 1:
        return im
    r, g, b, a = im.split()
    bicubic = image_mod.Resampling.BICUBIC
    red = r.resize((w + 2 * s, h + 2 * s), bicubic).crop((s, s, s + w, s + h))
    blue = b.copy()
    blue.paste(b.resize((w - 2 * s, h - 2 * s), bicubic), (s, s))
    return image_mod.merge("RGBA", (red, g, blue, a))


def _vignette(im: Any, e: Vignette) -> Any:
    image_mod, _, _ = _pil()
    # Pillow's radial gradient: 0 at the centre, reaching 255 at the edge midpoints of its square (and
    # clamped there, so 255 on and outside the inscribed circle); stretched to the window, 255 from its
    # inscribed ellipse out. So ``start`` and the full ``strength`` are measured to that ellipse, not to the
    # corners. Kept as it is: its bytes are pinned and approved frames were reviewed with this look
    # (hard rule 7); a corner-normalised falloff would be a new, separately reviewed effect.
    distance = image_mod.radial_gradient("L").resize(im.size, image_mod.Resampling.BILINEAR)

    def dark(v: int) -> int:
        t = (v / 255 - e.start) / (1 - e.start)
        t = min(1.0, max(0.0, t))
        return round(255 * e.strength * t * t * (3 - 2 * t))  # smoothstep

    return _darken(im, distance.point([dark(v) for v in range(256)]))
