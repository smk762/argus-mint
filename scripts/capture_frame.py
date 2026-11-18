"""Capture a frame overlay from a local HTML page, and write the frame's YAML beside it.

An overlay can be built as HTML and CSS (by hand, or from a framework-free component kit
vendored into the frame's source directory at a pinned commit, with its licence). This renders
the page to a transparent PNG at the frame's size with Playwright's Chromium, then writes the
frame YAML: the window and text slots from a small JSON spec, plus a comment header recording
how to rebuild the overlay. Comments aren't part of a frame's hash, so recording them changes
no hash; the PNG's bytes are what the hash covers.

    uv run --with playwright python scripts/capture_frame.py SPEC.json

The spec (JSON; paths relative to it)::

    {
      "html": "overlay.html",                       # the page; it may load only local files
      "png": "../harbour-feed-v1.png",              # the overlay to write
      "yaml": "../harbour-feed-v1.yaml",            # the frame to write (or update, while a draft)
      "width": 1024, "height": 1024,                # the overlay's size, in CSS pixels at scale 1
      "kit": {"name": "monohud", "commit": "<sha>", "license": "MIT"},   # optional: what the page vendors
      "frame": {"id": "<project>/<name>", "version": 1, "window": {...}, "texts": {...}, "style_locked": true}
    }

``frame`` is everything in the YAML but ``status`` (always ``draft``: approval is a review
step), ``overlay`` (the PNG's name) and the approval fields. An approved or retired frame is
never overwritten: a change is a new version, a new file.

No network: the page is opened from ``file://`` and every other request is refused, so a page
that reaches for a CDN fails to capture rather than capture something unrecorded. Recorded in
the YAML's header: the sha256 of the HTML and of every local file the page loaded (its CSS,
fonts, images), the kit's name and commit, and the Playwright and Chromium versions.

Playwright isn't a dependency of argus-mint (not even a test one): the renderer is injectable,
and the tests pass a stub. Install it for a capture with ``uv run --with playwright …`` and
``uv run --with playwright playwright install chromium-headless-shell``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import yaml

INSTALL = (
    "capture_frame needs Playwright, which argus-mint doesn't depend on. Run it with\n"
    "  uv run --with playwright python scripts/capture_frame.py SPEC.json\n"
    "after installing its browser once:\n"
    "  uv run --with playwright playwright install chromium-headless-shell"
)


class CaptureError(RuntimeError):
    """The spec, the page or the frame it would write is unusable."""


@dataclass
class Rendered:
    """What a renderer returns: the PNG, the local files the page loaded, and what rendered it."""

    png: bytes
    loaded: list[Path] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)  # e.g. {"playwright": "1.65.1", "chromium": "158…"}


Renderer = Callable[[Path, int, int], Rendered]


def playwright_render(html: Path, width: int, height: int) -> Rendered:
    """Render ``html`` at ``width`` x ``height`` (scale 1) to a PNG with a transparent background."""
    try:
        from importlib.metadata import version

        from playwright.sync_api import sync_playwright
    except ImportError:
        raise CaptureError(INSTALL) from None
    loaded: list[Path] = []
    refused: list[str] = []

    def route(r: Any) -> None:
        url = r.request.url
        if urlparse(url).scheme == "file":
            loaded.append(Path(unquote(urlparse(url).path)))
            r.continue_()
        else:
            refused.append(url)
            r.abort()

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # the browser isn't installed
            raise CaptureError(f"{e}\n\n{INSTALL}") from e
        try:
            ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
            ctx.route("**/*", route)
            page = ctx.new_page()
            page.goto(html.resolve().as_uri(), wait_until="load")
            page.evaluate("document.fonts.ready")
            png = page.screenshot(omit_background=True, full_page=False, animations="disabled")
            chromium = browser.version
        finally:
            browser.close()
    if refused:
        raise CaptureError(f"{html}: the page asked for {sorted(set(refused))}; only local files may be used")
    return Rendered(png=png, loaded=loaded, versions={"playwright": version("playwright"), "chromium": chromium})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _clean_png(png: bytes, width: int, height: int) -> bytes:
    """The capture as RGBA, at the spec's size, saved with no ancillary chunks."""
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        if im.size != (width, height):
            raise CaptureError(f"the capture is {im.size[0]}x{im.size[1]}, not {width}x{height}")
        rgba = im.convert("RGBA")
    out = io.BytesIO()
    rgba.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _header(spec_path: Path, yaml_path: Path, html: Path, sources: dict[str, str], kit: dict | None,
            versions: dict[str, str]) -> str:  # fmt: skip
    def rel(p: Path) -> str:
        return Path(os.path.relpath(p, yaml_path.parent)).as_posix()

    lines = [
        "# Captured by scripts/capture_frame.py; to rebuild the overlay:",
        f"#   uv run --with playwright python scripts/capture_frame.py {rel(spec_path)}",
        "# (paths here are relative to this file). Not part of the frame's hash: the overlay's bytes are.",
        f"# capture.html: {rel(html)}",
        f"# capture.html_sha256: {sources[rel(html)]}",
    ]
    lines += [f"# capture.source: {name} sha256 {digest}" for name, digest in sorted(sources.items())]
    if kit:
        lines.append(
            f"# capture.kit: {kit.get('name')} @ {kit.get('commit') or 'unpinned'}"
            + (f" ({kit['license']})" if kit.get("license") else "")
        )
    else:
        lines.append("# capture.kit: none (the page's own CSS)")
    lines += [f"# capture.{k}: {v}" for k, v in sorted(versions.items())]
    return "\n".join(lines) + "\n"


def _frame_yaml(header: str, frame: dict[str, Any], png_name: str) -> str:
    doc: dict[str, Any] = {"id": frame["id"], "version": frame["version"], "status": "draft", "overlay": png_name}
    doc |= {k: v for k, v in frame.items() if k not in ("id", "version")}

    def flow(v: Any) -> str:
        return yaml.safe_dump(v, default_flow_style=True, sort_keys=False, allow_unicode=True, width=10**6).strip()

    out = []
    for k, v in doc.items():  # as the hand-written frames are laid out: a box per line
        if isinstance(v, dict) and v and all(isinstance(x, dict) for x in v.values()):
            out.append(f"{k}:")
            out += [f"  {name}: {flow(box)}" for name, box in v.items()]
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):  # effects: one per line
            out.append(f"{k}:")
            out += [f"  - {flow(x)}" for x in v]
        elif isinstance(v, (dict, list)):
            out.append(f"{k}: {flow(v)}")
        else:
            out.append(yaml.safe_dump({k: v}, allow_unicode=True, width=10**6).strip())
    return header + "\n".join(out) + "\n"


def load_spec(spec_path: Path) -> dict[str, Any]:
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise CaptureError(f"{spec_path}: {e}") from e
    missing = sorted({"html", "png", "yaml", "width", "height", "frame"} - spec.keys())
    if missing:
        raise CaptureError(f"{spec_path}: missing {missing}")
    frame = spec["frame"]
    reserved = sorted({"status", "overlay", "approved_by", "approved_at"} & frame.keys())
    if reserved:
        raise CaptureError(f"{spec_path}: frame may not set {reserved} (the script writes them; approval is a review)")
    if not {"id", "version", "window"} <= frame.keys():
        raise CaptureError(f"{spec_path}: frame needs id, version and window")
    if not (isinstance(spec["width"], int) and isinstance(spec["height"], int)) or min(spec["width"], spec["height"]) < 1:
        raise CaptureError(f"{spec_path}: width and height are positive integers")
    return spec


def capture(spec_path: Path, renderer: Renderer = playwright_render) -> tuple[Path, Path]:
    """Render the spec's page and write its overlay PNG and frame YAML; return their paths.

    The YAML is validated with :func:`argus_mint.frames.load_frame` before it replaces anything;
    on failure the files are left as they were.
    """
    from argus_mint.frames import FrameError, load_frame

    spec_path = Path(spec_path).resolve()
    spec = load_spec(spec_path)
    base = spec_path.parent
    html = (base / spec["html"]).resolve()
    png_path = (base / spec["png"]).resolve()
    yaml_path = (base / spec["yaml"]).resolve()
    if not html.is_file():
        raise CaptureError(f"{html}: no such page")
    if png_path.parent != yaml_path.parent:
        raise CaptureError("the PNG and the YAML go in the same directory (a frame names its overlay beside it)")
    if yaml_path.is_file():
        current = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        if current.get("status", "draft") != "draft":
            raise CaptureError(
                f"{yaml_path.name} is {current.get('status')}: an approved or retired frame never changes; "
                "capture a new version instead"
            )

    width, height = spec["width"], spec["height"]
    out = renderer(html, width, height)
    png = _clean_png(out.png, width, height)

    def rel(p: Path) -> str:
        return Path(os.path.relpath(p, yaml_path.parent)).as_posix()

    sources = {rel(html): _sha256(html.read_bytes())}
    for f in out.loaded:
        f = f.resolve()
        if f.is_file():
            sources[rel(f)] = _sha256(f.read_bytes())
    header = _header(spec_path, yaml_path, html, sources, spec.get("kit"), out.versions)
    text = _frame_yaml(header, spec["frame"], png_path.name)

    old_png = png_path.read_bytes() if png_path.is_file() else None
    png_path.write_bytes(png)
    fd, tmp_name = tempfile.mkstemp(dir=yaml_path.parent, prefix=f".{yaml_path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            load_frame(tmp)
        except FrameError as e:
            if old_png is None:
                png_path.unlink(missing_ok=True)
            else:
                png_path.write_bytes(old_png)
            raise CaptureError(f"the frame doesn't validate, so nothing was written: {e}") from e
        os.replace(tmp, yaml_path)
    finally:
        tmp.unlink(missing_ok=True)
    return png_path, yaml_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("spec", type=Path, help="the capture spec (JSON)")
    args = ap.parse_args(argv)
    try:
        png, frame = capture(args.spec)
    except CaptureError as e:
        print(e, file=sys.stderr)
        return 1
    print(json.dumps({"png": str(png), "yaml": str(frame), "png_sha256": _sha256(png.read_bytes())}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
