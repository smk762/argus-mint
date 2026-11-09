"""Draw the placeholder example frame (frames/example-plain-v1.png).

A plain parchment border with a square art window and a name bar: something to test the
pipeline with until real marginalia art replaces it. Run: uv run python scripts/make_example_frame.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

W, H = 1000, 1400
WINDOW = (60, 60, 940, 940)  # x0, y0, x1, y1: transparent, the render shows through
NAME_BAR = (60, 980, 940, 1100)
PARCHMENT = (232, 217, 181, 255)
INK = (92, 64, 38, 255)


def main() -> None:
    im = Image.new("RGBA", (W, H), PARCHMENT)
    d = ImageDraw.Draw(im)
    d.rectangle((14, 14, W - 15, H - 15), outline=INK, width=6)
    d.rectangle((28, 28, W - 29, H - 29), outline=INK, width=2)
    d.rectangle(WINDOW, fill=(0, 0, 0, 0))
    d.rectangle((WINDOW[0] - 6, WINDOW[1] - 6, WINDOW[2] + 5, WINDOW[3] + 5), outline=INK, width=6)
    d.rectangle(NAME_BAR, outline=INK, width=3)
    out = Path(__file__).resolve().parents[1] / "frames" / "example-plain-v1.png"
    out.parent.mkdir(exist_ok=True)
    im.save(out, format="PNG", optimize=True)
    print(out)


if __name__ == "__main__":
    main()
