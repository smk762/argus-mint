"""A project's own frames (``<project>/frames/``, ids ``<project>/<name>``), and the capture script
that builds an overlay from a local HTML page (#62)."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from PIL import Image

from argus_mint.frames import FrameError, load_frame, project_frame_files
from argus_mint.projects import FrameLink, Ref, load_bundle, problems
from tests.test_teasers import (
    AUTH,
    FRAME_KEYS,
    LINKS,
    OWNER,
    PID,
    REPO,
    Stub,
    _make,
    app,  # noqa: F401 - a fixture
    choose,
    rendered_background,
    world,  # noqa: F401 - a fixture
)

EXAMPLE = REPO / "projects" / PID / "frames" / "harbour-feed-v1.yaml"
FRAME_ID = f"{PID}/harbour-feed"


def _capture_module():
    spec = importlib.util.spec_from_file_location("capture_frame", REPO / "scripts" / "capture_frame.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["capture_frame"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


capture_frame = _capture_module()


# -- naming --------------------------------------------------------------------------------------


def test_a_project_frame_is_named_by_its_project():
    lf = load_frame(EXAMPLE)
    assert lf.frame.id == FRAME_ID and lf.frame.project == PID and lf.ref().id == FRAME_ID
    assert project_frame_files(REPO / "projects") == [EXAMPLE]


def test_where_a_frame_lives_decides_its_scope(tmp_path):
    """A scoped id only in its own project's frames directory; an unscoped one never there."""
    project = tmp_path / "projects" / PID
    shutil.copytree(REPO / "projects" / PID, project)
    frames = project / "frames"
    as_global = tmp_path / "frames"
    as_global.mkdir()
    shutil.copy(frames / "harbour-feed-v1.png", as_global)
    shutil.copytree(frames / "fonts", as_global / "fonts")
    shutil.copy(frames / "harbour-feed-v1.yaml", as_global)
    with pytest.raises(FrameError, match="is a project's frame"):
        load_frame(as_global / "harbour-feed-v1.yaml")

    f = frames / "harbour-feed-v1.yaml"
    f.write_text(f.read_text().replace(f"id: {FRAME_ID}", "id: harbour-feed"))
    with pytest.raises(FrameError, match=f"a frame of project {PID} is named {PID}/<name>"):
        load_frame(f)
    f.write_text(f.read_text().replace("id: harbour-feed", "id: elsewhere/harbour-feed"))
    with pytest.raises(FrameError, match=f"is named {PID}/<name>, not 'elsewhere/harbour-feed'"):
        load_frame(f)
    for bad in ("a/b/c", "/x", "x/", "Upper/x"):
        f.write_text(re.sub(r"^id: .*$", f"id: {bad}", f.read_text(), flags=re.MULTILINE))
        with pytest.raises(FrameError):
            load_frame(f)


def test_frame_links_dump_as_refs_so_project_hashes_hold():
    """Project files written before project frames hash as before: a frame link dumps as a Ref."""
    assert FrameLink(id="example-teaser", version=1).model_dump() == Ref(id="example-teaser", version=1).model_dump()
    assert FrameLink(id=FRAME_ID, version=1).project == PID
    with pytest.raises(ValueError):
        Ref(id=FRAME_ID, version=1)  # lines, identities and scenes keep plain ids


def test_a_project_uses_global_frames_and_its_own_never_anothers(tmp_path):
    project = tmp_path / PID
    shutil.copytree(REPO / "projects" / PID, project)
    assert problems(load_bundle(project), FRAME_KEYS) == []
    other = "other-project/harbour-feed"
    for f, old in (("project.yaml", f"id: {FRAME_ID}"), ("identities/lamp-v1.yaml", f"id: {FRAME_ID}")):
        (project / f).write_text((project / f).read_text().replace(old, f"id: {other}"))
    doc = json.loads((project / LINKS).read_text())
    doc["teasers"]["T2"]["frame"]["id"] = other
    (project / LINKS).write_text(json.dumps(doc))
    found = problems(load_bundle(project), FRAME_KEYS | {(other, 1)})
    assert f"project.yaml names frame {other}, which is project other-project's" in found
    assert f"lamp-v1.yaml: frame {other} is project other-project's" in found
    assert f"{PID}-v1.json: T2: frame {other} is project other-project's" in found


# -- the teaser and the server --------------------------------------------------------------------


def test_a_viewpoint_teaser_pins_the_projects_frame_by_hash(world):  # noqa: F811
    root, frames, store = world
    bg, _ = rendered_background(store, root, "shoreline")
    choose(root, "T2", bg.id)
    made = _make(root, frames, store, "T2", renderer=Stub())
    lf = load_frame(root / "frames" / "harbour-feed-v1.yaml")
    assert made.draft.frame == lf.ref() and made.draft.frame.id == FRAME_ID  # type: ignore[union-attr]
    assert _make(root, frames, store, "T2", renderer=None).image == made.image


def test_project_frames_are_listed_served_and_reviewed_like_global_ones(app):  # noqa: F811
    build, _stub, root = app
    c = build()
    listed = {f["frame"]["id"]: f for f in c.get("/frames").json()}
    assert {"example-teaser", FRAME_ID} <= listed.keys()
    mine = listed[FRAME_ID]
    assert mine["project"] == PID and mine["file"] == f"{PID}/frames/harbour-feed-v1.yaml"
    assert listed["example-teaser"]["project"] is None
    overlay = (root / "frames" / "harbour-feed-v1.png").read_bytes()
    for path in (f"/frames/{FRAME_ID}/1/overlay", f"/frames/{PID}%2Fharbour-feed/1/overlay"):
        r = c.get(path)
        assert r.status_code == 200 and r.content == overlay, path
    font = c.get(f"/frames/{FRAME_ID}/1/fonts/fonts/CrimsonText-Italic.ttf")
    assert (
        font.status_code == 200 and font.content == (root / "frames" / "fonts" / "CrimsonText-Italic.ttf").read_bytes()
    )
    # a global frame's font: its id can't swallow the version ("example-teaser/1"), which is digits only
    glob = c.get("/frames/example-teaser/1/fonts/fonts/CrimsonText-Italic.ttf")
    assert glob.status_code == 200 and glob.content == font.content
    lay = c.post(f"/frames/{FRAME_ID}/1/layout", json={"texts": {"quote": "Every boat home."}})
    assert lay.status_code == 200 and lay.json()["width"] == 1024
    assert c.get("/frames/nobody/harbour-feed/1/overlay").status_code == 404

    # the copied project's frame was approved by the fixture; retire it through the frame route
    r = c.post(f"/frames/{FRAME_ID}/1/review", json={"decision": "retire", "by": "moderator"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "retired" and r.json()["id"] == FRAME_ID
    assert r.json()["file"] == f"{PID}/frames/harbour-feed-v1.yaml"
    assert "status: retired" in (root / "frames" / "harbour-feed-v1.yaml").read_text()
    assert r.json()["hash"] == mine["hash"]  # status isn't hashed
    again = c.post(f"/frames/{FRAME_ID}/1/review", json={"decision": "retire", "by": "m"}, headers=AUTH)
    assert again.status_code == 409


def test_a_teaser_draft_over_http_records_the_projects_frame(app):  # noqa: F811
    build, _stub, root = app
    c = build()
    r = c.post(f"/projects/{PID}/scenes/shoreline/1/backgrounds", headers=AUTH)
    assert r.status_code == 202
    for _ in range(200):
        s = c.get(f"/projects/{PID}/scenes/shoreline/1/backgrounds").json()
        if s["job"]["status"] in ("done", "failed"):
            break
    bg = s["backgrounds"][0]["id"]
    c.post(f"/projects/{PID}/backgrounds/{bg}/review", json={"decision": "approve", "by": "m"}, headers=AUTH)
    choose(root, "T2", bg)
    r = c.post(f"/projects/{PID}/teasers/T2/draft", json={"to": OWNER}, headers=AUTH)
    assert r.status_code == 201, r.text
    frame = r.json()["draft"]["frame"]
    listed = {f["frame"]["id"]: f for f in c.get("/frames").json()}
    assert frame["id"] == FRAME_ID and frame["hash"] == listed[FRAME_ID]["hash"]


def test_a_card_never_takes_a_projects_frame(app):  # noqa: F811
    build, *_ = app
    c = build()
    body = {
        "template": {"id": "example-portrait", "version": 3}, "variables": {}, "seed": 1, "asset": "a",
        "to": OWNER, "name": "n", "frame": {"id": FRAME_ID, "version": 1},
    }  # fmt: skip
    r = c.post("/drafts", json=body, headers=AUTH)
    assert r.status_code == 422 and "for its teasers only" in r.text


# -- the capture script ---------------------------------------------------------------------------


def _png(size: tuple[int, int]) -> bytes:
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    im.paste((10, 200, 10, 255), (0, 0, size[0], 40))
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


SPEC_FRAME = {
    "id": f"{PID}/test-feed",
    "version": 1,
    "window": {"x": 0, "y": 0, "width": 200, "height": 100},
    "style_locked": True,
    "texts": {"quote": {"x": 10, "y": 60, "width": 180, "height": 30, "colour": "#ffffff", "wrap": True}},
}


@pytest.fixture
def spec(tmp_path):
    """A project with a frame source: page, stylesheet and capture spec."""
    project = tmp_path / PID
    (project / "frames" / "src").mkdir(parents=True)
    (project / "project.yaml").write_text("id: x\n")  # its presence makes frames/ a project's
    src = project / "frames" / "src"
    (src / "page.html").write_text('<link rel="stylesheet" href="page.css"><div>REC</div>')
    (src / "page.css").write_text("div { color: green }")
    doc = {"html": "page.html", "png": "../test-feed-v1.png", "yaml": "../test-feed-v1.yaml", "width": 200,
           "height": 100, "kit": {"name": "somekit", "commit": "0123abc", "license": "MIT"}, "frame": SPEC_FRAME}  # fmt: skip
    (src / "capture.json").write_text(json.dumps(doc))
    return src / "capture.json"


def stub_renderer(size=(200, 100), calls=None):
    def render(html: Path, width: int, height: int):
        if calls is not None:
            calls.append((html, width, height))
        return capture_frame.Rendered(
            png=_png(size), loaded=[html.parent / "page.css"], versions={"playwright": "9.9", "chromium": "999.0"}
        )

    return render


def test_capture_writes_the_overlay_and_a_frame_that_records_its_sources(spec):
    calls: list = []
    png, frame = capture_frame.capture(spec, renderer=stub_renderer(calls=calls))
    assert calls == [(spec.parent / "page.html", 200, 100)]
    lf = load_frame(frame)
    assert lf.frame.id == f"{PID}/test-feed" and lf.frame.status == "draft" and lf.frame.overlay == png.name
    assert lf.size == (200, 100) and lf.frame.texts["quote"].wrap
    text = frame.read_text()
    html_sha = hashlib.sha256((spec.parent / "page.html").read_bytes()).hexdigest()
    css_sha = hashlib.sha256((spec.parent / "page.css").read_bytes()).hexdigest()
    assert f"# capture.html_sha256: {html_sha}" in text
    assert f"# capture.source: src/page.css sha256 {css_sha}" in text
    assert "# capture.kit: somekit @ 0123abc (MIT)" in text
    assert "# capture.chromium: 999.0" in text and "# capture.playwright: 9.9" in text
    assert "scripts/capture_frame.py src/capture.json" in text
    # what's recorded is comments: the hash is the frame's alone
    bare = frame.with_name("bare-v1.yaml")
    bare.write_text("".join(line for line in text.splitlines(keepends=True) if not line.startswith("#")))
    assert load_frame(bare).hash() == lf.hash()
    with Image.open(png) as im:
        assert im.mode == "RGBA" and im.size == (200, 100) and not im.info  # no ancillary chunks
    again_png, _ = capture_frame.capture(spec, renderer=stub_renderer())  # a draft is updated in place
    assert load_frame(frame).hash() == lf.hash() and again_png.read_bytes() == png.read_bytes()


def test_capture_never_overwrites_a_reviewed_frame(spec):
    _png_path, frame = capture_frame.capture(spec, renderer=stub_renderer())
    frame.write_text(
        frame.read_text().replace("status: draft", "status: approved\napproved_by: r\napproved_at: 2026-11-18")
    )
    before = frame.read_text(), (frame.parent / "test-feed-v1.png").read_bytes()
    with pytest.raises(capture_frame.CaptureError, match="never changes; capture a new version"):
        capture_frame.capture(spec, renderer=stub_renderer())
    assert (frame.read_text(), (frame.parent / "test-feed-v1.png").read_bytes()) == before


def test_capture_leaves_nothing_behind_when_the_frame_wont_validate(spec):
    with pytest.raises(capture_frame.CaptureError, match="is 50x50, not 200x100"):
        capture_frame.capture(spec, renderer=stub_renderer(size=(50, 50)))
    doc = json.loads(spec.read_text())
    doc["frame"] = {**SPEC_FRAME, "window": {"x": 0, "y": 0, "width": 400, "height": 100}}  # outside the overlay
    spec.write_text(json.dumps(doc))
    with pytest.raises(capture_frame.CaptureError, match="doesn't validate, so nothing was written"):
        capture_frame.capture(spec, renderer=stub_renderer())
    assert not list((spec.parent.parent).glob("test-feed-v1.*")) and not list(spec.parent.parent.glob(".*.tmp"))
    doc["frame"] = {**SPEC_FRAME, "status": "approved"}
    spec.write_text(json.dumps(doc))
    with pytest.raises(capture_frame.CaptureError, match="may not set"):
        capture_frame.capture(spec, renderer=stub_renderer())


def test_without_playwright_the_script_says_how_to_install_it(spec, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    assert capture_frame.main([str(spec)]) == 1
    err = capsys.readouterr().err
    assert "uv run --with playwright" in err and "playwright install chromium-headless-shell" in err


def test_the_example_frame_records_the_sources_it_was_captured_from():
    """The committed overlay can be rebuilt: every source the YAML names is on disk, unchanged."""
    text = EXAMPLE.read_text()
    sources = re.findall(r"^# capture\.source: (\S+) sha256 ([0-9a-f]{64})$", text, flags=re.MULTILINE)
    assert {name for name, _ in sources} >= {"src/harbour-feed/overlay.html", "src/harbour-feed/overlay.css"}
    for name, digest in sources:
        assert hashlib.sha256((EXAMPLE.parent / name).read_bytes()).hexdigest() == digest, name
    assert "# capture.kit: none" in text
    doc = yaml.safe_load(text)
    spec = json.loads((EXAMPLE.parent / "src" / "harbour-feed" / "capture.json").read_text())
    assert {k: doc[k] for k in spec["frame"]} == spec["frame"]
    assert doc["window"] == {"x": 0, "y": 0, "width": 1024, "height": 1024}  # full bleed: the background shows
    assert "http" not in (EXAMPLE.parent / "src" / "harbour-feed" / "overlay.css").read_text()
    assert "http" not in (EXAMPLE.parent / "src" / "harbour-feed" / "overlay.html").read_text()
