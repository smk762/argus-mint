"""Edge cases from the 2026-11 audit: the capture script, background ids, frame effects, catalogue
edits and whole floats in hashed metadata."""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest
import yaml
from PIL import Image

from argus_mint.backgrounds import Background
from argus_mint.catalogue import record_decision
from argus_mint.frame_effects import ChromaticAberration, apply_effects
from argus_mint.metadata import _whole_as_int
from tests.test_project_frames import SPEC_FRAME, capture_frame
from tests.test_teasers import PID, rendered_background, world  # noqa: F401 - a fixture

# -- the capture script ---------------------------------------------------------------------------


def _png(size=(200, 100), icc: bytes | None = None) -> bytes:
    im = Image.new("RGBA", size, (10, 200, 10, 255))
    out = io.BytesIO()
    im.save(out, format="PNG", **({"icc_profile": icc} if icc else {}))
    return out.getvalue()


def _chunks(png: bytes) -> list[bytes]:
    out, i = [], 8
    while i < len(png):
        n = int.from_bytes(png[i : i + 4], "big")
        out.append(png[i + 4 : i + 8])
        i += 12 + n
    return out


@pytest.fixture
def spec(tmp_path):
    project = tmp_path / PID
    src = project / "frames" / "src"
    src.mkdir(parents=True)
    (project / "project.yaml").write_text("id: x\n")
    (src / "page.html").write_text("<div>REC</div>")
    doc = {"html": "page.html", "png": "../test-feed-v1.png", "yaml": "../test-feed-v1.yaml", "width": 200,
           "height": 100, "frame": SPEC_FRAME}  # fmt: skip
    (src / "capture.json").write_text(json.dumps(doc))
    return src / "capture.json"


def renderer(png: bytes | None = None, loaded=()):
    def render(html: Path, width: int, height: int):
        return capture_frame.Rendered(png=png or _png(), loaded=[Path(p) for p in loaded], versions={})

    return render


def test_a_non_object_spec_or_frame_is_a_capture_error(spec):
    spec.write_text("[1, 2]")
    with pytest.raises(capture_frame.CaptureError, match="the spec is a JSON object"):
        capture_frame.load_spec(spec)
    doc = {"html": "a", "png": "b", "yaml": "c", "width": 1, "height": 1, "frame": "harbour"}
    spec.write_text(json.dumps(doc))
    with pytest.raises(capture_frame.CaptureError, match="frame is a JSON object"):
        capture_frame.load_spec(spec)


def test_the_frame_yaml_always_says_draft_and_names_the_new_overlay():
    text = capture_frame._frame_yaml("", SPEC_FRAME | {"status": "approved", "overlay": "old.png"}, "new.png")
    doc = yaml.safe_load(text)
    assert doc["status"] == "draft" and doc["overlay"] == "new.png"


def test_the_overlay_carries_no_icc_profile(spec):
    png, _ = capture_frame.capture(spec, renderer=renderer(_png(icc=b"\x00" * 128)))
    assert _chunks(png.read_bytes()) == [b"IHDR", b"IDAT", b"IEND"]


def test_the_frame_yaml_gets_the_files_usual_mode(spec):
    png, frame = capture_frame.capture(spec, renderer=renderer())
    assert stat.S_IMODE(frame.stat().st_mode) == stat.S_IMODE(png.stat().st_mode)
    os.chmod(frame, 0o640)
    _, frame = capture_frame.capture(spec, renderer=renderer())  # a draft updated keeps its mode
    assert stat.S_IMODE(frame.stat().st_mode) == 0o640


def test_a_file_loaded_from_outside_the_frame_is_refused(spec, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("key")
    with pytest.raises(capture_frame.CaptureError, match="outside"):
        capture_frame.capture(spec, renderer=renderer(loaded=[secret]))
    assert not (spec.parent.parent / "test-feed-v1.yaml").exists()
    # the frame's own directory (its shared fonts) is fine
    font = spec.parent.parent / "fonts" / "f.ttf"
    font.parent.mkdir()
    font.write_bytes(b"font")
    _, frame = capture_frame.capture(spec, renderer=renderer(loaded=[spec.parent / ".." / "fonts" / "f.ttf"]))
    assert "# capture.source: fonts/f.ttf sha256" in frame.read_text()


def test_the_route_check_resolves_dot_dot(tmp_path):
    roots = [tmp_path / "frames"]
    assert capture_frame._inside(tmp_path / "frames" / "src" / ".." / "fonts", roots)
    assert not capture_frame._inside(tmp_path / "frames" / ".." / "secret", roots)


# -- backgrounds ----------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../../other/x", "/abs/x", "a\\b", ""])
def test_a_background_id_never_names_a_path(world, bad):  # noqa: F811
    root, _, store = world
    bg, _ = rendered_background(store, root, "lamp-room", approved=False)
    with pytest.raises(ValueError, match="id is a file name"):
        Background.model_validate(bg.model_dump() | {"id": bad, "file": f"{bad}.png"})


# -- frame effects --------------------------------------------------------------------------------


@pytest.mark.parametrize("size", [(4, 4), (6, 40), (1, 1)])
def test_chromatic_aberration_never_crashes_on_a_small_image(size):
    im = Image.new("RGBA", size, (100, 150, 200, 255))
    out = apply_effects(im, (ChromaticAberration(type="chromatic_aberration", shift=16),), b"\x00" * 32)
    assert out.size == size


# -- catalogue ------------------------------------------------------------------------------------


def test_a_catalogue_edit_keeps_the_files_mode(tmp_path, template_doc):
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(template_doc | {"status": "draft"}, sort_keys=False))
    os.chmod(path, 0o644)
    from datetime import date

    record_decision(path, "approved", by="moderator", today=date(2026, 11, 21), validate=lambda p: None)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


# -- metadata -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "x,want",
    [(1.0, 1), (2.0**53, 2**53), (2.0**60, 1152921504606847000), (-(2.0**60), -1152921504606847000),
     (1e20, 10**20), (0.5, 0.5), (1e21, 1e21)],
)  # fmt: skip
def test_whole_floats_are_written_as_javascript_writes_them(x, want):
    got = _whole_as_int(x)
    assert got == want and type(got) is type(want)
    assert float(got) == x  # the same number, only its digits chosen as JavaScript's
