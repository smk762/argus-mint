"""Frame effects (#63): reviewed, deterministic changes to the art inside a frame's window."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
import yaml
from PIL import Image, ImageDraw

from argus_mint.frame_effects import NOISE_TAG
from argus_mint.frames import FrameError, compose, load_frame

from .test_frames import WINDOW, _frame

REPO = Path(__file__).resolve().parents[1]
HARBOUR = REPO / "projects" / "example-teasers" / "frames" / "harbour-feed-v1.yaml"

# The committed frames' hashes from before effects existed: a frame without effects keeps its hash.
HASHES_BEFORE_EFFECTS = {
    "frames/card-green-v1.yaml": "b5e88ede12f1d23493eddd6da8be0b8e224482e57b7edaa9479d5c13dc33167f",
    "frames/example-plain-v1.yaml": "de11b9747cc643c44849b5fbc3f1a4bc86c16968858280161bb2916382130856",
    "frames/example-teaser-v1.yaml": "3eef84dce41612126ec2339077d0d3e724a494fd9400812cb80d9933f3292c62",
}
HARBOUR_WITHOUT_EFFECTS = "2342792d7d65d7f0fd6200080f905345275eab0eec5041911065afabe46e2a65"  # its hash in #62

BARREL = {"type": "barrel", "strength": 0.3}
SCANLINES = {"type": "scanlines", "spacing": 4, "thickness": 2, "opacity": 0.5}
NOISE = {"type": "noise", "amount": 0.1, "monochrome": True}
COLOUR_NOISE = {"type": "noise", "amount": 0.1, "monochrome": False}
ABERRATION = {"type": "chromatic_aberration", "shift": 3}
VIGNETTE = {"type": "vignette", "strength": 0.8, "start": 0.3}
ALL = [BARREL, ABERRATION, SCANLINES, NOISE, VIGNETTE]


def _png(im: Image.Image) -> bytes:
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def _grid(size=(240, 240)) -> bytes:
    """Art with structure: a gradient, a grid and a bright disc, so each effect shows."""
    im = Image.new("RGB", size)
    d = ImageDraw.Draw(im)
    for y in range(size[1]):
        d.line([(0, y), (size[0] - 1, y)], fill=(40 + y // 3, 90, 160 - y // 3))
    for x in range(0, size[0], 24):
        d.line([(x, 0), (x, size[1] - 1)], fill=(240, 240, 240))
    for y in range(0, size[1], 24):
        d.line([(0, y), (size[0] - 1, y)], fill=(240, 240, 240))
    d.ellipse((150, 140, 210, 180), fill=(250, 230, 160))
    return _png(im)


FLAT = _png(Image.new("RGB", (240, 240), (120, 120, 120)))
GRID = _grid()


def _card(tmp_path, effects, art=GRID) -> Image.Image:
    lf = load_frame(_frame(tmp_path, effects=effects))
    return Image.open(io.BytesIO(compose(lf, art))).convert("RGBA")


def _window(im: Image.Image) -> Image.Image:
    return im.crop((WINDOW["x"], WINDOW["y"], WINDOW["x"] + WINDOW["width"], WINDOW["y"] + WINDOW["height"]))


def _outside_identical(a: Image.Image, b: Image.Image) -> bool:
    """Every pixel outside the window is the same in both."""
    a, b = a.copy(), b.copy()
    for im in (a, b):
        im.paste(
            (0, 0, 0, 0), (WINDOW["x"], WINDOW["y"], WINDOW["x"] + WINDOW["width"], WINDOW["y"] + WINDOW["height"])
        )
    return a.tobytes() == b.tobytes()


# -- the hash --------------------------------------------------------------------------------------


@pytest.mark.parametrize("rel", sorted(HASHES_BEFORE_EFFECTS))
def test_committed_frames_without_effects_keep_their_hash(rel):
    lf = load_frame(REPO / rel)
    assert not lf.frame.effects
    assert lf.hash().hex() == HASHES_BEFORE_EFFECTS[rel]


def test_the_example_frame_without_its_effects_hashes_as_it_did(tmp_path):
    frames = tmp_path / "example-teasers" / "frames"
    frames.mkdir(parents=True)
    (tmp_path / "example-teasers" / "project.yaml").write_text("id: example-teasers\n")
    for f in ("harbour-feed-v1.png",):
        (frames / f).write_bytes((HARBOUR.parent / f).read_bytes())
    (frames / "fonts").mkdir()
    for f in (HARBOUR.parent / "fonts").iterdir():
        (frames / "fonts" / f.name).write_bytes(f.read_bytes())
    doc = yaml.safe_load(HARBOUR.read_text())
    assert doc["effects"]
    with_effects = load_frame(HARBOUR).hash().hex()
    del doc["effects"]
    (frames / "harbour-feed-v1.yaml").write_text(yaml.safe_dump(doc))
    assert load_frame(frames / "harbour-feed-v1.yaml").hash().hex() == HARBOUR_WITHOUT_EFFECTS != with_effects


def test_effects_are_part_of_the_hash_order_and_parameters_included(tmp_path):
    h = [
        load_frame(_frame(tmp_path, effects=e)).hash()
        for e in ([], [NOISE], [NOISE, VIGNETTE], [VIGNETTE, NOISE], [NOISE | {"amount": 0.11}])
    ]
    assert len(set(h)) == len(h)
    assert load_frame(_frame(tmp_path, effects=[])).hash() == load_frame(_frame(tmp_path)).hash()


# -- validation ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        [{"type": "barrel", "strength": 0.6}],
        [{"type": "barrel", "strength": 0}],
        [{"type": "scanlines", "spacing": 4, "thickness": 4, "opacity": 0.5}],
        [{"type": "scanlines", "spacing": 4, "thickness": 1, "opacity": 1.5}],
        [{"type": "noise", "amount": 0.6, "monochrome": True}],
        [{"type": "noise", "amount": 0.1}],  # every parameter is given: nothing is left to a default
        [{"type": "chromatic_aberration", "shift": 17}],
        [{"type": "vignette", "strength": 0.5, "start": 1.0}],
        [{"type": "vignette", "strength": 0.5, "start": 0.5, "radius": 2}],  # an unknown parameter
        [{"type": "blur", "radius": 2}],  # an unknown effect
        [NOISE] * 9,  # at most eight
    ],
)
def test_effect_parameters_are_fixed_and_bounded(tmp_path, bad):
    with pytest.raises(FrameError):
        load_frame(_frame(tmp_path, effects=bad))


def test_an_effect_too_large_for_its_window_is_refused(tmp_path):
    with pytest.raises(FrameError, match="shift 16 is too large for a 40x40 window"):
        load_frame(_frame(tmp_path, effects=[{"type": "chromatic_aberration", "shift": 16}],
                          window={"x": 30, "y": 30, "width": 40, "height": 40}))  # fmt: skip


# -- determinism -------------------------------------------------------------------------------------


def test_composing_twice_gives_the_same_bytes(tmp_path):
    lf = load_frame(_frame(tmp_path, effects=ALL))
    first = compose(lf, GRID, {"name": "Harbour", "bio": "A light on the water."})
    assert compose(lf, GRID, {"name": "Harbour", "bio": "A light on the water."}) == first
    assert (
        compose(load_frame(_frame(tmp_path, effects=ALL)), GRID, {"name": "Harbour", "bio": "A light on the water."})
        == first
    )


# Pinned bytes: the same Pillow (uv.lock, requirements-test.txt) gives the same PNG in any process, on any
# machine (these were computed in separate processes).
# A change here changes the bytes of every card made with effects (hard rule 7): re-pin only on purpose.
PINNED = {
    "barrel": "277143ee55a70a161aefb052508776db230552bd5915a1dcf6dd4959fcd803fc",
    "scanlines": "32646fc4fac492285bbb9212753007b89d08661853b189d4f1dbc91c6dc1dfd6",
    "noise": "1a65a56133ba3fbe4340e9aac8fdf76a26525c8ebf8fe0c870cd3986934f1339",
    "chromatic_aberration": "9cba9df530c6f17e97252531131ea5cd7c11ff1974e3152353fb781dcf1738be",
    "vignette": "d47752c2084845e64b76399ae49567381c0564463068652c779c257129f1f5bc",
    "all": "6a288e0bb92c5b54fa65ca9884152f304918afb882522cc1f45940479e80d8dd",
}


@pytest.mark.parametrize(
    "name,effects",
    [("barrel", [BARREL]), ("scanlines", [SCANLINES]), ("noise", [NOISE]),
     ("chromatic_aberration", [ABERRATION]), ("vignette", [VIGNETTE]), ("all", ALL)],
)  # fmt: skip
def test_each_effect_is_byte_stable_and_pinned(tmp_path, name, effects):
    lf = load_frame(_frame(tmp_path, effects=effects))
    png = compose(lf, GRID)
    assert compose(lf, GRID) == png
    assert hashlib.sha256(png).hexdigest() == PINNED[name]


# -- each effect does what it says, inside the window only --------------------------------------------


def test_effects_change_only_the_window(tmp_path):
    plain = _card(tmp_path, [])
    for effects in ([BARREL], [SCANLINES], [NOISE], [ABERRATION], [VIGNETTE], ALL):
        framed = _card(tmp_path, effects)
        assert _outside_identical(plain, framed), effects
        assert _window(plain).tobytes() != _window(framed).tobytes(), effects


def test_barrel_keeps_the_corners_and_magnifies_the_centre(tmp_path):
    ramp = Image.new("RGB", (240, 240))
    ramp.putdata([(x, y, 100) for y in range(240) for x in range(240)])  # each pixel names where it is
    plain, bulged = _window(_card(tmp_path, [], art=_png(ramp))), _window(_card(tmp_path, [BARREL], art=_png(ramp)))
    for corner in ((1, 1), (238, 1), (1, 238), (238, 238)):  # the corners stay where they are
        assert max(abs(a - b) for a, b in zip(plain.getpixel(corner), bulged.getpixel(corner), strict=True)) <= 2
    # nearer the middle, a pixel shows the art from nearer the centre: r * (1 + k r^2) / (1 + k)
    x, y = bulged.getpixel((24, 110))[:2]
    r2 = (96**2 + 10**2) / (2 * 120**2)
    f = (1 + 0.3 * r2) / 1.3
    assert abs(x - (120 - 96 * f)) <= 2 and abs(y - (120 - 10 * f)) <= 2 and x > 24
    plain_grid, bulged_grid = _window(_card(tmp_path, [])), _window(_card(tmp_path, [BARREL]))
    assert (
        plain_grid.getpixel((24, 110))[:3] == (240, 240, 240) != bulged_grid.getpixel((24, 110))[:3]
    )  # a line moved out


def test_scanlines_darken_their_rows_and_leave_the_gaps(tmp_path):
    w = _window(_card(tmp_path, [SCANLINES], art=FLAT))
    for y in range(12):
        expected = round(120 * (1 - 0.5)) if y % 4 < 2 else 120
        assert abs(w.getpixel((50, y))[0] - expected) <= 1, y


def test_noise_is_bounded_seeded_by_the_art_and_monochrome_when_asked(tmp_path):
    grey = _window(_card(tmp_path, [NOISE], art=FLAT))
    diffs = [v - 120 for v in grey.getchannel("R").tobytes()]
    assert max(abs(d) for d in diffs) <= round(0.1 * 255) and len(set(diffs)) > 10
    assert abs(sum(diffs) / len(diffs)) < 1.5  # centred
    assert grey.getchannel("R").tobytes() == grey.getchannel("G").tobytes() == grey.getchannel("B").tobytes()
    colour = _window(_card(tmp_path, [COLOUR_NOISE], art=FLAT))
    assert colour.getchannel("R").tobytes() != colour.getchannel("B").tobytes()
    other_art = _png(Image.new("RGB", (240, 240), (121, 120, 120)))  # one value off: another sha256
    other = _window(_card(tmp_path, [NOISE], art=other_art))
    assert [v - 120 for v in other.getchannel("G").tobytes()] != [v - 120 for v in grey.getchannel("G").tobytes()]
    # the seed: SHAKE-256 of the tag, the art's sha256, the effect's place and the channel
    seed = hashlib.shake_256(NOISE_TAG + hashlib.sha256(FLAT).digest() + bytes([0, 0])).digest(240 * 240)
    up = [(v, d) for v, d in zip(seed, diffs, strict=True) if v >= 200]
    assert up and all(d > 0 for _v, d in up)


def test_chromatic_aberration_moves_red_and_blue_and_leaves_green(tmp_path):
    plain, fringed = _window(_card(tmp_path, [])), _window(_card(tmp_path, [ABERRATION]))
    assert plain.getchannel("G").tobytes() == fringed.getchannel("G").tobytes()
    assert plain.getchannel("R").tobytes() != fringed.getchannel("R").tobytes()
    assert plain.getchannel("B").tobytes() != fringed.getchannel("B").tobytes()
    # red at the centre doesn't move (it spreads from there)
    assert plain.getpixel((120, 120))[0] == fringed.getpixel((120, 120))[0]


def test_vignette_darkens_the_corners_and_spares_the_centre(tmp_path):
    w = _window(_card(tmp_path, [VIGNETTE], art=FLAT))
    assert w.getpixel((w.width // 2, w.height // 2))[:3] == (120, 120, 120)
    corner = w.getpixel((0, 0))[0]
    assert abs(corner - round(120 * (1 - 0.8))) <= 3


def test_the_example_frame_has_mild_effects():
    lf = load_frame(HARBOUR)
    kinds = [e.type for e in lf.frame.effects]
    assert kinds == ["barrel", "chromatic_aberration", "scanlines", "noise", "vignette"]
    by = {e.type: e for e in lf.frame.effects}
    assert by["barrel"].strength <= 0.15 and by["noise"].amount <= 0.08 and by["scanlines"].opacity <= 0.25
