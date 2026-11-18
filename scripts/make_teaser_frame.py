"""Draw the example full-bleed teaser frame (frames/example-teaser-v1.png).

The art fills the whole 1024x1024 canvas (window = the canvas). The overlay is transparent
except for a dark band that fades in under the quote and a small dark tab behind the
"generated image" label, so white text stays legible on any background. Slots `quote`,
`attribution` and `label` are in frames/example-teaser-v1.yaml.
Run: uv run python scripts/make_teaser_frame.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

W = H = 1024
BAND_TOP, BAND_FULL = 600, 780  # the band fades in from BAND_TOP and is at full strength from BAND_FULL
BAND_ALPHA = 190
LABEL_TAB = (760, 20, 1004, 64)


def main() -> None:
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for y in range(BAND_TOP, H):
        a = BAND_ALPHA * min(1.0, (y - BAND_TOP) / (BAND_FULL - BAND_TOP))
        d.line([(0, y), (W - 1, y)], fill=(0, 0, 0, round(a)))
    d.rounded_rectangle(LABEL_TAB, radius=10, fill=(0, 0, 0, 150))
    out = Path(__file__).resolve().parents[1] / "frames" / "example-teaser-v1.png"
    out.parent.mkdir(exist_ok=True)
    im.save(out, format="PNG", optimize=True)
    print(out)


if __name__ == "__main__":
    main()
