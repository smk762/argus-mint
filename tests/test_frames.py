"""Frames: validation, hashing, compositing, and framed cards through draft → review → publish."""

from __future__ import annotations

import io
import json

import pytest
import yaml
from PIL import Image
from typer.testing import CliRunner

from argus_mint.cli import app
from argus_mint.frames import FrameError, compose, fit_text, load_frame, require_approved
from argus_mint.metadata import Provenance

from .test_cli import approve, make_draft, publish_args

runner = CliRunner()
WINDOW = {"x": 30, "y": 30, "width": 240, "height": 240}


def _png(size, colour, window=None) -> bytes:
    im = Image.new("RGBA", size, colour)
    if window:
        im.paste(
            (0, 0, 0, 0), (window["x"], window["y"], window["x"] + window["width"], window["y"] + window["height"])
        )
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _frame(tmp_path, *, status="approved", name=True, **over):
    (tmp_path / "f.png").write_bytes(_png((300, 420), (200, 180, 140, 255), WINDOW))
    doc = {"id": "test-frame", "version": 1, "status": status, "overlay": "f.png", "window": WINDOW}
    if status != "draft":
        doc |= {"approved_by": "reviewer", "approved_at": "2026-11-09"}
    if name:
        doc["texts"] = {
            "name": {"x": 10, "y": 300, "width": 280, "height": 60, "size": 40, "colour": "#102030"},
            "bio": {"x": 10, "y": 362, "width": 280, "height": 50, "size": 20, "min_size": 10, "wrap": True},
        }
    doc |= over
    path = tmp_path / "f.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


ART = _png((64, 64), (0, 0, 255, 255))


def test_compose_puts_art_in_the_window_under_the_overlay(tmp_path):
    f = load_frame(_frame(tmp_path))
    out = Image.open(io.BytesIO(compose(f, ART, {"name": "Hana"}))).convert("RGBA")
    assert out.size == (300, 420)
    assert out.getpixel((150, 150)) == (0, 0, 255, 255)  # art shows through the window
    assert out.getpixel((2, 2)) == (200, 180, 140, 255)  # overlay elsewhere
    assert any(
        out.getpixel((x, y)) != (200, 180, 140, 255) for x in range(10, 290) for y in range(300, 360)
    )  # the name is drawn
    assert compose(f, ART, {"name": "Hana"}) == compose(f, ART, {"name": "Hana"})  # deterministic


def test_text_slots_shrink_wrap_and_clip(tmp_path):
    f = load_frame(_frame(tmp_path))
    plain = compose(f, ART)  # slots may be left empty
    assert compose(f, ART, {"name": "Hana"}) != plain
    with pytest.raises(FrameError, match="no text slot"):
        compose(f, ART, {"stats": "3/2"})

    from PIL import ImageFont

    slot = f.frame.texts["bio"]
    load = ImageFont.load_default
    font, lines = fit_text(load, "A short bio.", slot)
    assert lines == ["A short bio."] and font.size == slot.size
    long = "word " * 200
    font, lines = fit_text(load, long, slot)
    assert font.size == slot.min_size and lines[-1].endswith("…")  # clipped at the smallest size
    assert all(font.getlength(line) <= slot.width for line in lines)
    name_slot = f.frame.texts["name"]
    font, lines = fit_text(load, "an extremely long name that cannot possibly fit on one line at all", name_slot)
    assert len(lines) == 1 and lines[0].endswith("…") and font.getlength(lines[0]) <= name_slot.width


def test_frame_validation(tmp_path):
    with pytest.raises(FrameError, match="outside"):
        load_frame(_frame(tmp_path, window={"x": 150, "y": 150, "width": 240, "height": 240}))
    with pytest.raises(FrameError, match="approved_by"):
        load_frame(_frame(tmp_path, approved_by=None))
    with pytest.raises(FrameError, match="cannot read"):
        load_frame(_frame(tmp_path, overlay="missing.png"))
    with pytest.raises(FrameError, match="only approved frames"):
        require_approved(load_frame(_frame(tmp_path, status="draft")))


def test_frame_hash_covers_the_overlay_but_not_status(tmp_path):
    a = load_frame(_frame(tmp_path))
    retired = load_frame(_frame(tmp_path, status="retired"))
    assert a.hash() == retired.hash()
    (tmp_path / "f.png").write_bytes(_png((300, 420), (1, 2, 3, 255), WINDOW))
    changed = load_frame(tmp_path / "f.yaml")
    assert changed.hash() != a.hash()


def test_repo_example_frames_are_valid():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "frames"
    for f in root.glob("*.yaml"):
        assert load_frame(f).frame.status in ("draft", "approved", "retired")


def test_framed_card_records_frame_and_art(tmp_path, template_doc, monkeypatch):
    frame = _frame(tmp_path)
    image = tmp_path / "render.png"
    image.write_bytes(ART)
    out, d = make_draft(tmp_path, template_doc, "--image", str(image), "--frame", str(frame))
    assert d["image"].endswith("card.png") and (out / "card.png").exists()
    approve(out)
    monkeypatch.setenv("ARGUS_MINT_SIGNER_KEY", "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d")
    result = runner.invoke(app, publish_args(out, "--dry-run"))
    assert result.exit_code == 0, result.output
    pub = json.loads(result.stdout)
    prov = pub["metadata"]["provenance"]
    assert prov["frame"]["id"] == "test-frame" and prov["frame"]["hash"] == "0x" + load_frame(frame).hash().hex()
    assert prov["art"] == f"ipfs://{pub['render']['art_cid']}"
    assert pub["metadata"]["image"] == f"ipfs://{pub['render']['image_cid']}" != prov["art"]


def test_draft_refuses_a_draft_frame(tmp_path, template_doc):
    frame = _frame(tmp_path, status="draft")
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(template_doc))
    from .test_cli import draft_args

    result = runner.invoke(app, draft_args(path, tmp_path / "d", "--placeholder", "--frame", str(frame)))
    assert result.exit_code == 1 and "only approved frames" in result.output


def test_provenance_frame_and_art_go_together():
    base = {
        "template": "t", "template_version": 1, "template_hash": "0x" + "11" * 32, "seed": 1, "attempts": 1,
        "asset_id": "0x" + "22" * 32, "consumer": "c",
    }  # fmt: skip
    assert "frame" not in Provenance(**base).to_json()  # unframed cards hash as before
    with pytest.raises(ValueError, match="go together"):
        Provenance(**base, frame={"id": "f", "version": 1, "hash": "0x" + "33" * 32})


def test_existing_demo_metadata_still_hashes_to_its_voucher():
    from pathlib import Path

    from argus_mint.metadata import content_hash

    root = Path(__file__).resolve().parents[1] / "demo"
    for f in [*root.glob("base-sepolia/mint-*.json"), *root.glob("base/mint-*.json")]:
        out = json.loads(f.read_text())
        assert Provenance.model_validate(out["metadata"]["provenance"]).to_json() == out["metadata"]["provenance"]
        assert "0x" + content_hash(out["metadata"]).hex() == out["voucher"]["content_hash"]


def test_style_tints_and_paints_slots_deterministically(tmp_path):
    from argus_mint.frames import tint_matrix
    from argus_mint.metadata import CardStyle

    f = load_frame(_frame(tmp_path))
    style = CardStyle.model_validate(
        {
            "tint": {"colour": "#ff0000", "strength": 1.0},
            "slots": {
                "name": {"colour": "#00ff00", "effect": "glow", "effect_colour": "#0000ff",
                         "background": {"colour": "#123456", "opacity": 1, "radius": 0, "padding": 0}},
                "bio": {"effect": "shadow"},
            },
        }
    )  # fmt: skip
    plain = Image.open(io.BytesIO(compose(f, ART, {"name": "Hana"}))).convert("RGBA")
    styled_png = compose(f, ART, {"name": "Hana", "bio": "x"}, style)
    assert styled_png == compose(f, ART, {"name": "Hana", "bio": "x"}, style)
    styled = Image.open(io.BytesIO(styled_png)).convert("RGBA")
    r, g, b, _ = styled.getpixel((2, 2))  # the overlay, tinted fully red at its own brightness
    assert r > g and r > b and plain.getpixel((2, 2)) != styled.getpixel((2, 2))
    assert styled.getpixel((11, 301)) == (0x12, 0x34, 0x56, 255)  # the name slot's background corner
    assert styled.getpixel((150, 150)) == plain.getpixel((150, 150))  # the art isn't tinted
    assert tint_matrix(None) == (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0)
    assert tint_matrix(style.tint)[:4] == (0.299, 0.587, 0.114, 0.0)


def test_locked_frames_refuse_styles(tmp_path):
    from argus_mint.metadata import CardStyle

    f = load_frame(_frame(tmp_path, style_locked=True))
    with pytest.raises(FrameError, match="can't be restyled"):
        compose(f, ART, {}, CardStyle.model_validate({"tint": {"colour": "#000000"}}))
    assert compose(f, ART, {}, CardStyle())  # a plain style is no style
