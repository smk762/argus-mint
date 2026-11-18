"""Backgrounds and teaser drafts: the pure parts, darkroom's inpainting request, and the routes (stub darkroom)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import struct
import time
import zlib
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml
from eth_account import Account
from fastapi.testclient import TestClient
from PIL import Image

from argus_mint.backgrounds import (
    Background,
    BackgroundError,
    BackgroundStore,
    background_id,
    background_seed,
    parse_background_id,
    render_background,
    scene_request,
)
from argus_mint.cards import CardDraft, DraftError
from argus_mint.darkroom import DarkroomClient, DarkroomError, check_inpaint, check_render, request_body
from argus_mint.frames import load_frame
from argus_mint.projects import (
    Area,
    GuardVerdict,
    Identity,
    Line,
    Project,
    Scene,
    TeaserLinks,
    load_bundle,
    text_sha256,
)
from argus_mint.render import GenResult, InitImage, Rendered, RenderFailed
from argus_mint.server import Settings, create_app
from argus_mint.spoilers import EMPTY, parse
from argus_mint.teasers import (
    GENERATED_LABEL,
    INPAINT_DENOISE,
    MAX_LORAS,
    TeaserError,
    TeaserNotFound,
    bake_png,
    inpaint_request,
    make_teaser,
    mask_png,
    png_chunks,
    teaser_loras,
    teaser_seed,
    teaser_texts,
)
from argus_mint.templates import LoraRef
from argus_mint.workflow import edit_text, publish
from tests.test_darkroom import FakeDarkroom

REPO = Path(__file__).resolve().parents[1]
PID = "example-teasers"
TOKEN = "s3cret-moderator"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
OWNER_KEY = Account.from_key("0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d")
OWNER = OWNER_KEY.address
CRITICAL = {b"IHDR", b"PLTE", b"IDAT", b"IEND"}


def png(width: int, height: int, colour: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, format="PNG")
    return out.getvalue()


LINKS = Path("teasers") / f"{PID}-v1.json"
MODELS = {"project": Project, "identities": Identity, "scenes": Scene, "lines": Line, "teasers": TeaserLinks}


def approve(root: Path, *, skip: tuple[str, ...] = (), refuse: str | None = None) -> None:
    """Approve every file of the copied example project (but those named in ``skip``), with a passing
    SingGuard verdict stored for each text (refused for any text containing ``refuse``)."""
    for f in [root / "project.yaml", *sorted(root.glob("*/*.yaml")), root / LINKS]:
        rel = f.relative_to(root).as_posix()
        if rel in skip:
            continue
        doc = json.loads(f.read_text()) if f.suffix == ".json" else yaml.safe_load(f.read_text())
        doc |= {"status": "approved", "approved_by": "tester", "approved_at": "2026-11-18", "guard": []}
        obj = MODELS["project" if rel == "project.yaml" else rel.split("/")[0]].model_validate(doc)
        doc["guard"] = [
            GuardVerdict(field=k, sha256=text_sha256(t), policy="default", passed=not (refuse and refuse in t),
                         detail="rule-7" if refuse and refuse in t else "", checked_at=datetime.now(UTC),
                         ).model_dump(mode="json")
            for k, t in obj.guarded_texts()
        ]  # fmt: skip
        f.write_text(json.dumps(doc) if f.suffix == ".json" else yaml.safe_dump(doc, sort_keys=False))


def approve_frame(frames: Path) -> Path:
    for f in ("example-teaser-v1.yaml", "example-teaser-v1.png", "example-plain-v1.yaml", "example-plain-v1.png"):
        shutil.copy(REPO / "frames" / f, frames / f)
    shutil.copytree(REPO / "frames" / "fonts", frames / "fonts")
    f = frames / "example-teaser-v1.yaml"
    f.write_text(
        f.read_text().replace("status: draft", "status: approved\napproved_by: tester\napproved_at: 2026-11-18")
    )
    return f


def choose(root: Path, number: str, bg: str | None) -> None:
    """Choose a background (in tests, by editing the approved links in place; really, a new version)."""
    doc = json.loads((root / LINKS).read_text())
    doc["teasers"][number]["background"] = bg
    (root / LINKS).write_text(json.dumps(doc))


class Stub:
    """Stands in for darkroom: renders a flat colour per seed, inpaints by filling the mask's white area."""

    def __init__(self, *outcomes: str, inpaint: list[str] | None = None, render: list[str] | None = None):
        self.outcomes = list(outcomes)
        self.render = render
        self.render_asks: list[tuple[str, int]] = []
        self.calls: list[tuple[object, InitImage | None]] = []
        self.priority: str | None = None
        self.inpaint = inpaint
        self.parent: Stub | None = None

    def batch(self) -> Stub:
        b = Stub()
        b.priority, b.parent, b.outcomes = "batch", self, self.outcomes
        return b

    def check_render(self, model: str, loras: int) -> list[str]:
        self.render_asks.append((model, loras))
        return [] if self.render is None else self.render

    def check_inpaint(self, model: str, loras: int) -> list[str]:
        return [] if self.inpaint is None else self.inpaint

    def check_text(self, text, policy=None):
        return True, ""

    def generate(self, req, init: InitImage | None = None) -> GenResult:
        (self.parent or self).calls.append((req, init))
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "rejected":
            return GenResult.safety_rejected("deleted: default:A", ref=f"rec{req.seed}")
        if outcome == "error":
            return GenResult.error("comfyui down", ref=f"rec{req.seed}")
        if init is None:
            return GenResult.ok(
                png(req.width, req.height, (req.seed % 256, 40, 90)), ref=f"rec{req.seed}", version="0.3.0"
            )
        base = Image.open(io.BytesIO(init.image)).convert("RGB")
        mask = Image.open(io.BytesIO(init.mask)).convert("L")  # type: ignore[arg-type]
        base.paste(Image.new("RGB", base.size, (200, 180, 20)), mask=mask)
        out = io.BytesIO()
        base.save(out, format="PNG")
        return GenResult.ok(out.getvalue(), ref=f"inp{req.seed}", version="0.3.0")


@pytest.fixture
def world(tmp_path):
    """The example project, approved, with an approved frame and a background store."""
    root = tmp_path / "projects" / PID
    shutil.copytree(REPO / "projects" / PID, root)
    approve(root)
    frames = tmp_path / "frames"
    frames.mkdir()
    approve_frame(frames)
    return root, frames, BackgroundStore(tmp_path / "backgrounds")


def rendered_background(store: BackgroundStore, root: Path, scene_id: str, n: int = 1, *, approved=True):
    b = load_bundle(root)
    scene = b.scenes[(scene_id, 1)][0]
    r = render_background(Stub(), b.project, scene, n)
    bg = store.add(PID, scene, n, r, {"argus-mint": "test", "darkroom": "0.3.0"})
    if approved:
        bg = store.review(PID, bg.id, approve=True, by="moderator")
    return bg, store.image(PID, bg.id)


# -- the PNG ---------------------------------------------------------------------------------------


def _with_chunk(data: bytes, kind: bytes, body: bytes) -> bytes:
    chunk = struct.pack(">I4s", len(body), kind) + body + struct.pack(">I", zlib.crc32(kind + body))
    return data[:33] + chunk + data[33:]  # after IHDR


def test_bake_drops_every_ancillary_chunk_and_is_byte_stable():
    src = _with_chunk(png(64, 48, (10, 20, 30)), b"tEXt", b"Comment\x00made somewhere")
    src = _with_chunk(src, b"tIME", b"\x07\xea\x0b\x12\x00\x00\x00")
    assert {k for k, _ in png_chunks(src)} - CRITICAL == {b"tEXt", b"tIME"}
    baked = bake_png(src)
    assert {k for k, _ in png_chunks(baked)} <= CRITICAL
    assert baked == bake_png(src) == bake_png(baked)  # stripping again changes no byte
    assert Image.open(io.BytesIO(baked)).mode == "RGB"


def test_png_chunks_refuses_a_broken_file():
    with pytest.raises(ValueError, match="not a PNG"):
        png_chunks(b"GIF89a")
    data = bytearray(png(8, 8, (0, 0, 0)))
    data[20] ^= 0xFF  # inside IHDR
    with pytest.raises(ValueError, match="CRC"):
        png_chunks(bytes(data))


def test_mask_is_white_over_the_area_only_and_the_same_every_time():
    area = Area(x=10, y=20, width=30, height=40)
    m = mask_png(100, 80, area)
    assert m == mask_png(100, 80, area)
    assert {k for k, _ in png_chunks(m)} <= CRITICAL
    im = Image.open(io.BytesIO(m))
    assert im.size == (100, 80) and im.mode == "L"
    assert im.getpixel((10, 20)) == im.getpixel((39, 59)) == 255
    assert im.getpixel((9, 20)) == im.getpixel((40, 59)) == im.getpixel((10, 60)) == 0
    with pytest.raises(TeaserError, match="outside"):
        mask_png(30, 30, area)


# -- the parts ------------------------------------------------------------------------------------


def test_background_ids_and_seeds_are_fixed_by_the_scene():
    b = load_bundle(REPO / "projects" / PID)
    scene = b.scenes[("lamp-room", 1)][0]
    assert background_id(scene, 2) == "lamp-room-v1-02"
    assert parse_background_id("lamp-room-v1-02") == ("lamp-room", 1, 2)
    for bad in ("lamp-room-02", "../x-v1-01", "lamp-room-v0-01", "lamp-room-v1-1"):
        with pytest.raises(BackgroundError):
            parse_background_id(bad)
    assert background_seed(PID, scene, 1) == background_seed(PID, scene, 1) != background_seed(PID, scene, 2)
    req = scene_request(scene, 7)
    assert (req.template_id, req.template_version, req.template_hash) == ("lamp-room", 1, scene.hash())
    assert req.prompt == scene.prompt and req.loras == () and req.init_image is None and req.variables == {}


def test_backgrounds_render_only_from_approved_files(tmp_path):
    root = tmp_path / PID
    shutil.copytree(REPO / "projects" / PID, root)
    b = load_bundle(root)
    with pytest.raises(BackgroundError, match="only approved scenes"):
        render_background(Stub(), b.project, b.scenes[("lamp-room", 1)][0], 1)
    approve(root, skip=("scenes/lamp-room-v1.yaml",))
    b = load_bundle(root)
    with pytest.raises(BackgroundError, match="scene lamp-room v1 is draft"):
        render_background(Stub(), b.project, b.scenes[("lamp-room", 1)][0], 1)
    with pytest.raises(BackgroundError, match="asks for 3"):
        render_background(Stub(), b.project, b.scenes[("shoreline", 1)][0], 4)


def test_a_rejected_background_is_retried_with_the_next_seed_and_every_attempt_kept(world):
    root, _, store = world
    b = load_bundle(root)
    scene = b.scenes[("lamp-room", 1)][0]
    stub = Stub("rejected", "ok")
    r = render_background(stub, b.project, scene, 1)
    assert [a.outcome for a in r.attempts] == ["safety_rejected", "ok"]
    assert {c[0].prompt for c in stub.calls} == {scene.prompt}  # the prompt never changes
    assert r.attempts[0].seed == background_seed(PID, scene, 1)
    with pytest.raises(RenderFailed):
        render_background(Stub(*["rejected"] * scene.max_attempts), b.project, scene, 2)


def test_store_records_reviews_once_and_checks_the_image(world):
    root, _, store = world
    bg, image = rendered_background(store, root, "lamp-room", approved=False)
    s = store.load(PID, "lamp-room", 1)
    assert s is not None and s.missing() == [2, 3] and bg.status == "pending"
    with pytest.raises(BackgroundError, match="already rendered"):
        store.add(PID, load_bundle(root).scenes[("lamp-room", 1)][0], 1, Rendered(image, bg.request), {})
    decided = store.review(PID, bg.id, approve=False, by="mod", note="too dark")
    assert decided.status == "rejected" and decided.review.image_sha256 == hashlib.sha256(image).hexdigest()
    with pytest.raises(BackgroundError, match="already rejected"):
        store.review(PID, bg.id, approve=True, by="mod")
    (store.dir(PID, "lamp-room", 1) / f"{bg.id}.png").write_bytes(b"tampered")
    with pytest.raises(BackgroundError, match="differs"):
        store.image(PID, bg.id)
    with pytest.raises(BackgroundError):
        store.dir("../etc", "lamp-room", 1)


def test_a_finally_refused_background_isnt_rendered_again_but_an_error_is(world):
    root, _, store = world
    scene = load_bundle(root).scenes[("shoreline", 1)][0]
    store.fail(PID, scene, 1, "safety", True, [])
    store.fail(PID, scene, 2, "comfyui down", False, [])
    assert store.load(PID, "shoreline", 1).missing() == [2, 3]  # type: ignore[union-attr]


def test_texts_go_in_their_slots_and_a_missing_slot_is_refused(world):
    root, frames, _ = world
    b = load_bundle(root)
    lf = load_frame(frames / "example-teaser-v1.yaml")
    line = b.lines[("keep-it-lit", 1)][0]
    assert teaser_texts(lf, b.project, line) == {"quote": line.shown, "label": GENERATED_LABEL}
    signed = b.project.model_copy(update={"attribution": "— the keeper"})
    assert teaser_texts(lf, signed, line)["attribution"] == "— the keeper"
    unlabelled = b.project.model_copy(update={"generated_label": False})
    assert "label" not in teaser_texts(lf, unlabelled, line)
    with pytest.raises(TeaserError, match="no label, quote slot"):
        teaser_texts(load_frame(frames / "example-plain-v1.yaml"), b.project, line)


def test_an_embodied_render_takes_the_identity_lora_and_the_style_lora_and_no_more():
    b = load_bundle(REPO / "projects" / PID)
    keeper = b.identities[("keeper", 1)][0]
    style = LoraRef(name="lantern-style.safetensors", weight=0.7)
    pinned = keeper.model_copy(update={"lora": LoraRef(name="keeper.safetensors"), "proof_run": "run-1"})
    project = b.project.model_copy(update={"style_lora": style})
    assert teaser_loras(b.project, keeper) == ()
    assert [x.name for x in teaser_loras(project, pinned)] == ["keeper.safetensors", "lantern-style.safetensors"]
    assert len(teaser_loras(project, pinned)) <= MAX_LORAS == 2
    with pytest.raises(TeaserError, match="same LoRA"):
        teaser_loras(project, pinned.model_copy(update={"lora": style}))
    scene = b.scenes[("lamp-room", 1)][0]
    init = InitImage(png(8, 8, (0, 0, 0)), png(8, 8, (255, 255, 255)), INPAINT_DENOISE)
    req = inpaint_request(project, pinned, scene, init.ref(), 9)
    assert (req.template_id, req.template_hash) == ("keeper", pinned.hash())
    assert (req.model, req.width, req.height, req.sampling) == (scene.model, scene.width, scene.height, scene.sampling)
    assert req.prompt == keeper.prompt and req.init_image == init.ref() and len(req.loras) == 2
    with pytest.raises(TeaserError, match="viewpoint"):
        inpaint_request(project, b.identities[("lamp", 1)][0], scene, init.ref(), 9)


# -- darkroom's inpainting request ------------------------------------------------------------------


class InpaintingDarkroom(FakeDarkroom):
    """FakeDarkroom that records the input image and mask as darkroom does (``request.init_image``)."""

    def __init__(self, *outcomes, wrong_mask: bool = False):
        super().__init__(*outcomes)
        self.wrong_mask = wrong_mask

    def _stream(self, body, outcome):
        resp = super()._stream(body, outcome)
        init = body.get("init_image")
        if init is None:
            return resp
        sha = lambda b64: hashlib.sha256(base64.b64decode(b64)).hexdigest()  # noqa: E731
        mask = "00" * 32 if self.wrong_mask else sha(init["mask_base64"])
        record_init = {"source_sha256": sha(init["data_base64"]), "denoise": init["denoise"],
                       "mask": {"source_sha256": mask, "masked_fraction": 0.3}}  # fmt: skip
        text = resp.text.replace('"workflow_sha256"', f'"init_image": {json.dumps(record_init)}, "workflow_sha256"')
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})


def _inpaint_parts():
    b = load_bundle(REPO / "projects" / PID)
    scene = b.scenes[("lamp-room", 1)][0]
    init = InitImage(png(1024, 1024, (1, 2, 3)), mask_png(1024, 1024, scene.character_area), INPAINT_DENOISE)  # type: ignore[arg-type]
    return inpaint_request(b.project, b.identities[("keeper", 1)][0], scene, init.ref(), 11), init


def test_darkroom_gets_the_input_image_and_mask_the_request_records():
    req, init = _inpaint_parts()
    body = request_body(req, init=init)
    assert base64.b64decode(body["init_image"]["data_base64"]) == init.image
    assert base64.b64decode(body["init_image"]["mask_base64"]) == init.mask
    assert body["init_image"]["denoise"] == INPAINT_DENOISE and body["expand"] is False
    with pytest.raises(DarkroomError, match="records"):
        request_body(req)  # the request names an input image: it can't go without it
    with pytest.raises(DarkroomError, match="records"):
        request_body(req, init=InitImage(b"other", init.mask, INPAINT_DENOISE))


@pytest.mark.parametrize(("wrong_mask", "ok"), [(False, True), (True, False)])
def test_darkroom_client_checks_the_record_names_what_was_sent(wrong_mask, ok):
    req, init = _inpaint_parts()
    fake = InpaintingDarkroom("passed", wrong_mask=wrong_mask)
    dr = DarkroomClient("http://darkroom:8106", client=httpx.Client(transport=httpx.MockTransport(fake)))
    result = dr.generate(req, init=init)
    assert (result.outcome == "ok") is ok
    if not ok:
        assert "different mask" in result.detail


def test_check_inpaint_reads_darkrooms_inpainting_workflows():
    model = "sdxl/juggernautXL_ragnarokBy/juggernautXL_ragnarokBy.safetensors"
    models = {"checkpoints": [{"name": model, "inpaint_lora_counts": [0, 1]}]}
    assert check_inpaint(models, model, 1) == []
    assert "takes 2 LoRA(s)" in check_inpaint(models, model, 2)[0]  # darkroom's examples stop at sdxl+lora1+inpaint
    assert "no such checkpoint" in check_inpaint(models, "flux/x.safetensors", 0)[0]
    assert "doesn't say" in check_inpaint({"checkpoints": [{"name": model}]}, model, 0)[0]


# -- the draft --------------------------------------------------------------------------------------


def _make(root, frames, store, number="T1", *, bg=None, renderer=None, spoilers=EMPTY):
    b = load_bundle(root)
    _s, background = store.find(PID, bg or json.loads((root / LINKS).read_text())["teasers"][number]["background"])
    return make_teaser(
        bundle=b, number=number, frame=load_frame(frames / "example-teaser-v1.yaml"), background=background,
        background_image=store.image(PID, background.id), to=OWNER, renderer=renderer, spoilers=spoilers,
        frames={("example-teaser", 1), ("example-plain", 1)},
    )  # fmt: skip


def test_an_embodied_teaser_inpaints_the_identity_onto_the_approved_background(world):
    root, frames, store = world
    bg, bg_image = rendered_background(store, root, "lamp-room")
    choose(root, "T1", bg.id)
    stub = Stub()
    made = _make(root, frames, store, renderer=stub)
    d = made.draft
    assert d.kind == "teaser" and d.asset_id is None and d.consumer is None and d.bio is None and d.to == OWNER
    ((req, init),) = stub.calls
    assert init is not None and init.image == bg_image and init.denoise == INPAINT_DENOISE
    assert req.init_image == init.ref() and req.template_id == "keeper" and req.loras == ()
    assert req.seed == teaser_seed(PID, "T1", bg.sha256)
    b = load_bundle(root)
    t = d.teaser
    assert t is not None and t.number == "T1" and t.presence == "embodied"
    assert t.background.id == bg.id and t.background.sha256 == bg.sha256
    assert t.line.hash == "0x" + b.lines[("keep-it-lit", 1)][0].hash().hex()
    assert t.project.id == PID and t.scene.id == "lamp-room" and t.identity.id == "keeper"
    assert d.texts == {"quote": b.lines[("keep-it-lit", 1)][0].shown, "label": GENERATED_LABEL}
    assert d.frame is not None and d.frame.id == "example-teaser"
    assert d.made_with["darkroom"] == "0.3.0" and d.renderer == "darkroom"
    assert {k for k, _ in png_chunks(made.image)} <= CRITICAL
    assert (
        d.image_sha256 == hashlib.sha256(made.image).hexdigest()
        and d.art_sha256 == hashlib.sha256(made.art).hexdigest()
    )
    art = Image.open(io.BytesIO(made.art)).convert("RGB")
    area = b.scenes[("lamp-room", 1)][0].character_area
    assert art.getpixel((area.x + 5, area.y + 5)) == (200, 180, 20)  # painted in
    assert art.getpixel((5, 5)) == Image.open(io.BytesIO(bg_image)).convert("RGB").getpixel((5, 5))  # kept
    again = _make(root, frames, store, renderer=Stub())
    assert again.image == made.image  # byte-stable: the same parts make the same PNG
    assert CardDraft.model_validate_json(d.model_dump_json()) == d


def test_a_viewpoint_teaser_uses_the_background_itself_under_its_frame(world):
    root, frames, store = world
    bg, bg_image = rendered_background(store, root, "shoreline")
    choose(root, "T2", bg.id)
    stub = Stub()
    made = _make(root, frames, store, "T2", renderer=stub)
    assert stub.calls == [] and made.art == bg_image
    assert made.draft.request == bg.request and made.draft.teaser.presence == "viewpoint"  # type: ignore[union-attr]
    assert made.draft.made_with["darkroom"] == "0.3.0"
    assert _make(root, frames, store, "T2", renderer=None).image == made.image  # no darkroom needed


def test_refusals(world):
    root, frames, store = world
    bg, _ = rendered_background(store, root, "lamp-room")
    pending, _ = rendered_background(store, root, "lamp-room", 2, approved=False)
    with pytest.raises(TeaserNotFound):
        _make(root, frames, store, "T9", bg=bg.id, renderer=Stub())
    with pytest.raises(TeaserError, match="no background chosen"):
        _make(root, frames, store, bg=bg.id, renderer=Stub())
    choose(root, "T1", pending.id)
    with pytest.raises(TeaserError, match="is pending; only approved backgrounds"):
        _make(root, frames, store, renderer=Stub())
    choose(root, "T1", bg.id)
    with pytest.raises(TeaserError, match="not lamp-room-v1-02"):
        _make(root, frames, store, bg=pending.id, renderer=Stub())
    with pytest.raises(TeaserError, match="held by spoiler rule 2"):
        _make(root, frames, store, renderer=Stub(), spoilers=parse("nothing here\nbrass lens\n"))
    with pytest.raises(TeaserError, match="needs darkroom"):
        _make(root, frames, store, renderer=None)
    with pytest.raises(TeaserError, match="can't inpaint this teaser: no workflow"):
        _make(root, frames, store, renderer=Stub(inpaint=["no workflow"]))
    stub = Stub()
    _make(root, frames, store, renderer=stub)
    assert len(stub.calls) == 1

    line = root / "lines" / "keep-it-lit-v1.yaml"
    line.write_text(line.read_text().replace("status: approved", "status: draft"))
    with pytest.raises(TeaserError, match="line keep-it-lit v1 is draft"):
        _make(root, frames, store, renderer=Stub())
    approve(root)
    teasers = json.loads((root / LINKS).read_text())
    teasers["teasers"]["T1"]["line"] = {"id": "every-boat", "version": 1}  # the lamp's voice, not the keeper's
    (root / LINKS).write_text(json.dumps(teasers))
    with pytest.raises(TeaserError, match="the project has problems"):
        _make(root, frames, store, renderer=Stub())


def test_a_frame_that_isnt_approved_is_refused(world):
    root, frames, store = world
    bg, _ = rendered_background(store, root, "lamp-room")
    choose(root, "T1", bg.id)
    f = frames / "example-teaser-v1.yaml"
    f.write_text(f.read_text().replace("status: approved", "status: draft"))
    with pytest.raises(TeaserError, match="frame example-teaser v1 is draft"):
        _make(root, frames, store, renderer=Stub())


def test_a_teaser_draft_has_no_card_fields_and_isnt_edited_or_published_as_a_card(world):
    root, frames, store = world
    bg, _ = rendered_background(store, root, "lamp-room")
    choose(root, "T1", bg.id)
    made = _make(root, frames, store, renderer=Stub())
    d = made.draft
    with pytest.raises(ValueError, match="no asset_id"):
        CardDraft.model_validate(d.model_dump() | {"asset_id": "0x" + "11" * 32})
    with pytest.raises(ValueError, match="needs its teaser record"):
        CardDraft.model_validate(d.model_dump() | {"teaser": None})
    with pytest.raises(ValueError, match="needs its asset_id"):
        CardDraft.model_validate(d.model_dump() | {"kind": "card"})
    with pytest.raises(DraftError, match="no card provenance"):
        d.provenance(None)
    with pytest.raises(DraftError, match="reviewed line"):
        edit_text(d, made.art, load_frame(frames / "example-teaser-v1.yaml"), role="owner", by=OWNER, name="x")
    # Publishing is optional and comes after both approvals (test_posting has the teaser's metadata).
    with pytest.raises(DraftError, match="still needs approval"):
        publish(d, made.art, made.image, pinner=None, signer=None, chain_id=1, card="0x0")  # type: ignore[arg-type]


def test_card_drafts_load_as_before():
    """A draft written before teasers (no kind) is a card, unchanged."""
    fixture = next((REPO / "fixtures" / "api" / "drafts").glob("*.json"))
    doc = json.loads(fixture.read_text())["draft"]
    doc.pop("kind", None)
    doc.pop("teaser", None)
    d = CardDraft.model_validate(doc)
    assert d.kind == "card" and d.asset_id is not None and d.teaser is None


# -- the routes --------------------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path, world):
    root, frames, _ = world
    stub = Stub()

    def build(**over):
        darkroom = over.pop("darkroom", stub)
        cfg = Settings(**({
            "drafts_dir": tmp_path / "drafts", "samples_dir": tmp_path / "samples",
            "templates_dir": REPO / "templates", "frames_dir": frames, "bios_dir": REPO / "bios",
            "projects_dir": root.parent, "backgrounds_dir": tmp_path / "backgrounds", "moderator_token": TOKEN,
            "siwe_domain": "localhost:3108",
        } | over))  # fmt: skip
        return TestClient(create_app(cfg, darkroom=darkroom, probe_fn=lambda url: False))

    return build, stub, root


def _wait(c, path):
    for _ in range(200):
        s = c.get(path).json()
        if s["job"]["status"] in ("done", "failed"):
            return s
        time.sleep(0.02)
    raise AssertionError("the background job didn't finish")


def test_background_job_review_and_teaser_draft_over_http(app):
    build, stub, root = app
    c = build()
    caps = c.get("/health").json()["capabilities"]
    assert caps["backgrounds"] and caps["teasers"]
    url = f"/projects/{PID}/scenes/lamp-room/1/backgrounds"
    assert c.post(url).status_code == 401
    r = c.post(url, headers=AUTH)
    assert r.status_code == 202, r.text
    assert r.json()["job"]["numbers"] == [1, 2, 3]
    s = _wait(c, url)
    assert s["job"]["status"] == "done" and [b["id"] for b in s["backgrounds"]] == [
        "lamp-room-v1-01", "lamp-room-v1-02", "lamp-room-v1-03"
    ]  # fmt: skip
    assert all(b["status"] == "pending" for b in s["backgrounds"])
    assert {req.prompt for req, _ in stub.calls} == {load_bundle(root).scenes[("lamp-room", 1)][0].prompt}
    assert c.post(url, headers=AUTH).status_code == 409  # nothing left to render
    listed = c.get(f"/projects/{PID}/backgrounds").json()["sets"]
    assert [x["scene"] for x in listed] == ["lamp-room"]
    image = c.get(f"/projects/{PID}/backgrounds/lamp-room-v1-02/image")
    assert image.status_code == 200 and hashlib.sha256(image.content).hexdigest() == s["backgrounds"][1]["sha256"]

    review = f"/projects/{PID}/backgrounds/lamp-room-v1-02/review"
    assert c.post(review, json={"decision": "approve", "by": "m"}).status_code == 401
    r = c.post(review, json={"decision": "approve", "by": "m"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert c.post(review, json={"decision": "reject", "by": "m"}, headers=AUTH).status_code == 409
    assert c.post(f"/projects/{PID}/backgrounds/nope-v1-01/review", json={"decision": "approve", "by": "m"},
                  headers=AUTH).status_code == 404  # fmt: skip

    draft_url = f"/projects/{PID}/teasers/T1/draft"
    assert c.post(draft_url, json={"to": OWNER}, headers=AUTH).status_code == 409  # no background chosen
    choose(root, "T1", "lamp-room-v1-01")
    r = c.post(draft_url, json={"to": OWNER}, headers=AUTH)
    assert r.status_code == 409 and "pending" in r.json()["detail"]
    choose(root, "T1", "lamp-room-v1-02")
    assert c.post(draft_url, json={"to": OWNER}).status_code == 401
    r = c.post(draft_url, json={"to": OWNER}, headers=AUTH)
    assert r.status_code == 201, r.text
    detail = r.json()
    assert detail["status"] == "pending" and detail["awaiting"] == ["moderator", "owner"]
    assert detail["draft"]["kind"] == "teaser" and detail["draft"]["teaser"]["background"]["id"] == "lamp-room-v1-02"
    summary = c.get("/drafts").json()["items"][0]
    assert summary["kind"] == "teaser" and summary["teaser"] == {"project": PID, "number": "T1"}
    image = c.get(f"/drafts/{detail['id']}/image").content
    assert {k for k, _ in png_chunks(image)} <= CRITICAL
    r = c.post(f"/drafts/{detail['id']}/review", json={"role": "moderator", "approve": True, "by": "m"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["awaiting"] == ["owner"]  # then the owner signs, as for a card
    assert c.post(f"/projects/{PID}/teasers/T99/draft", json={"to": OWNER}, headers=AUTH).status_code == 404
    assert c.post(f"/projects/{PID}/teasers/x/draft", json={"to": OWNER}, headers=AUTH).status_code == 404


def test_background_jobs_run_at_batch_priority_and_only_for_approved_scenes(app, tmp_path):
    build, stub, root = app
    c = build()
    scene = root / "scenes" / "shoreline-v1.yaml"
    scene.write_text(scene.read_text().replace("status: approved", "status: draft"))
    r = c.post(f"/projects/{PID}/scenes/shoreline/1/backgrounds", headers=AUTH)
    assert r.status_code == 409 and "draft" in r.json()["detail"]
    assert c.post(f"/projects/{PID}/scenes/nope/1/backgrounds", headers=AUTH).status_code == 404
    assert c.post("/projects/nope/scenes/shoreline/1/backgrounds", headers=AUTH).status_code == 404

    seen: list[str | None] = []

    class Recording(Stub):
        def batch(self):
            b = super().batch()
            seen.append(b.priority)
            return b

    c2 = build(darkroom=Recording(), backgrounds_dir=tmp_path / "bg2")
    assert c2.post(f"/projects/{PID}/scenes/lamp-room/1/backgrounds", headers=AUTH).status_code == 202
    _wait(c2, f"/projects/{PID}/scenes/lamp-room/1/backgrounds")
    assert seen == ["batch"]

    no_darkroom = TestClient(create_app(Settings(
        drafts_dir=tmp_path / "d3", projects_dir=root.parent, backgrounds_dir=tmp_path / "bg3",
        templates_dir=REPO / "templates", frames_dir=REPO / "frames", bios_dir=REPO / "bios", moderator_token=TOKEN,
    ), probe_fn=lambda url: False))  # fmt: skip
    assert no_darkroom.get("/health").json()["capabilities"]["backgrounds"] is False
    assert no_darkroom.post(f"/projects/{PID}/scenes/lamp-room/1/backgrounds", headers=AUTH).status_code == 503


def test_a_failed_render_is_recorded_and_retried_by_the_next_job(app, tmp_path):
    build, _, _root = app
    c = build(darkroom=Stub("error", "ok", "ok"), backgrounds_dir=tmp_path / "bg4")
    url = f"/projects/{PID}/scenes/lamp-room/1/backgrounds"
    c.post(url, headers=AUTH)
    s = _wait(c, url)
    assert [b["n"] for b in s["backgrounds"]] == [2, 3]
    assert s["failed"][0]["n"] == 1 and s["failed"][0]["final"] is False and "comfyui" in s["failed"][0]["error"]
    assert c.post(url, headers=AUTH).json()["job"]["numbers"] == [1]
    s = _wait(c, url)
    assert [b["n"] for b in s["backgrounds"]] == [1, 2, 3] and s["failed"] == []


def test_an_interrupted_job_reads_as_failed(world, tmp_path):
    root, _, store = world
    scene = load_bundle(root).scenes[("lamp-room", 1)][0]
    from argus_mint.backgrounds import BackgroundJob
    from argus_mint.server_teasers import BackgroundRunner

    store.set_job(PID, scene, BackgroundJob(id="ab" * 8, status="running", created_at=datetime.now(UTC), numbers=(1,)))
    runner = BackgroundRunner(store, root.parent)
    shown = runner.shown(store.load(PID, "lamp-room", 1))  # type: ignore[arg-type]
    assert shown.job.status == "failed" and "interrupted" in shown.job.error  # type: ignore[union-attr,operator]
    assert not runner.busy(shown)


def test_background_model_keeps_status_and_review_together(world):
    root, _, store = world
    bg, _ = rendered_background(store, root, "lamp-room", approved=False)
    with pytest.raises(ValueError, match="doesn't match the review"):
        Background.model_validate(bg.model_dump() | {"status": "approved"})
    with pytest.raises(ValueError, match="file is"):
        Background.model_validate(bg.model_dump() | {"file": "../../x.png"})


def test_only_approved_teaser_links_with_fresh_singguard_verdicts_make_teasers(world):
    root, frames, store = world
    bg, _ = rendered_background(store, root, "lamp-room")
    choose(root, "T1", bg.id)
    made = _make(root, frames, store, renderer=Stub())
    links = load_bundle(root).current_teasers()
    assert links is not None and made.draft.teaser.links.hash == "0x" + links.hash().hex()  # type: ignore[union-attr]

    approve(root, refuse="lighthouse keeper")  # the keeper's prompt was refused
    choose(root, "T1", bg.id)
    with pytest.raises(TeaserError, match="identity keeper v1: prompt was refused"):
        _make(root, frames, store, renderer=Stub())

    approve(root)
    line = root / "lines" / "keep-it-lit-v1.yaml"
    doc = yaml.safe_load(line.read_text())
    doc["guard"] = [v for v in doc["guard"] if v["field"] != "published"]  # never judged
    line.write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(TeaserError, match="published has no verdict"):
        _make(root, frames, store, renderer=Stub())

    approve(root, skip=(LINKS.as_posix(),))
    links_file = root / LINKS
    doc = json.loads(links_file.read_text())
    doc["status"] = "draft"
    doc.pop("approved_by", None)
    doc.pop("approved_at", None)
    links_file.write_text(json.dumps(doc))
    with pytest.raises(TeaserError, match="no approved teaser links"):
        _make(root, frames, store, bg=bg.id, renderer=Stub())


def test_the_tracker_reports_each_teasers_stages_from_the_files_and_drafts(app):
    from eth_account.messages import encode_defunct

    build, _stub, root = app
    c = build()
    t = c.get(f"/projects/{PID}/tracker").json()
    assert t["links_version"] == 1 and [x["number"] for x in t["teasers"]] == ["T1", "T2"]
    assert t["teasers"][0]["stages"] == {
        "quote_final": True, "concept_chosen": True, "image_made": False, "posted": False, "minted": False
    }  # fmt: skip
    assert t["teasers"][0]["drafts"] == [] and t["teasers"][0]["background"] is None

    url = f"/projects/{PID}/scenes/lamp-room/1/backgrounds"
    c.post(url, headers=AUTH)
    _wait(c, url)
    c.post(f"/projects/{PID}/backgrounds/lamp-room-v1-01/review", json={"decision": "approve", "by": "m"}, headers=AUTH)
    choose(root, "T1", "lamp-room-v1-01")
    draft_id = c.post(f"/projects/{PID}/teasers/T1/draft", json={"to": OWNER}, headers=AUTH).json()["id"]
    first = c.get(f"/projects/{PID}/tracker").json()["teasers"][0]
    assert first["stages"]["image_made"] is False and first["background"] == "lamp-room-v1-01"
    (state,) = first["drafts"]
    assert state["id"] == draft_id and state["status"] == "pending" and state["awaiting"] == ["moderator", "owner"]
    assert state["current"] is True and state["links_version"] == 1

    c.post(f"/drafts/{draft_id}/review", json={"role": "moderator", "approve": True, "by": "m"}, headers=AUTH)
    ch = c.post(f"/drafts/{draft_id}/challenge", json={"approve": True}).json()
    sig = OWNER_KEY.sign_message(encode_defunct(text=ch["message"])).signature.hex()
    r = c.post(f"/drafts/{draft_id}/review", json={"role": "owner", "approve": True, "message": ch["message"],
                                                   "signature": sig})  # fmt: skip
    assert r.status_code == 200 and r.json()["status"] == "approved"
    first = c.get(f"/projects/{PID}/tracker").json()["teasers"][0]
    assert first["stages"]["image_made"] is True and first["stages"]["posted"] is False
    assert c.post(f"/drafts/{draft_id}/challenge", json={"action": "edit", "edit": {"name": "x"}}).status_code == 409

    line = root / "lines" / "keep-it-lit-v1.yaml"  # retired: the quote is no longer final
    line.write_text(line.read_text().replace("status: approved", "status: retired"))
    assert c.get(f"/projects/{PID}/tracker").json()["teasers"][0]["stages"]["quote_final"] is False
    assert c.get("/projects/nope/tracker").status_code == 404


def test_backgrounds_take_the_projects_style_lora_when_it_has_one(app, tmp_path):
    build, _stub, root = app
    style = {"name": "lantern-style.safetensors", "weight": 0.7, "trigger_words": ["lantern style"]}
    project = root / "project.yaml"
    doc = yaml.safe_load(project.read_text())
    project.write_text(yaml.safe_dump(doc | {"style_lora": style}, sort_keys=False))
    approve(root)  # re-judge the project's texts (its hash changed)

    refusing = Stub(render=["no workflow renders it with 1 LoRA(s) (it has [0])"])
    c = build(darkroom=refusing, backgrounds_dir=tmp_path / "bg-refused")
    r = c.post(f"/projects/{PID}/scenes/lamp-room/1/backgrounds", headers=AUTH)
    assert r.status_code == 409 and "with 1 LoRA" in r.json()["detail"]
    assert refusing.calls == [] and refusing.render_asks[0][1] == 1

    stub = Stub()
    c = build(darkroom=stub, backgrounds_dir=tmp_path / "bg-styled")
    url = f"/projects/{PID}/scenes/lamp-room/1/backgrounds"
    assert c.post(url, headers=AUTH).status_code == 202
    s = _wait(c, url)
    assert all(b["request"]["loras"] == [style] for b in s["backgrounds"])  # recorded in each request
    assert {tuple(x.name for x in req.loras) for req, _ in stub.calls} == {("lantern-style.safetensors",)}
    assert stub.render_asks == [(load_bundle(root).scenes[("lamp-room", 1)][0].model, 1)]


def test_without_a_style_lora_backgrounds_take_none():
    b = load_bundle(REPO / "projects" / PID)
    assert b.project.style_lora is None
    assert scene_request(b.scenes[("lamp-room", 1)][0], 1).loras == ()


def test_check_render_reads_darkrooms_workflows():
    model = "sdxl/juggernautXL_ragnarokBy/juggernautXL_ragnarokBy.safetensors"
    models = {"checkpoints": [{"name": model, "lora_counts": [0, 1, 2]}]}
    assert check_render(models, model, 1) == []
    assert "with 1 LoRA" in check_render({"checkpoints": [{"name": model, "lora_counts": [0]}]}, model, 1)[0]
    assert "no such checkpoint" in check_render(models, "flux/x.safetensors", 0)[0]
