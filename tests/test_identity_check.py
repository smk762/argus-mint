"""The identity check on a teaser (#51): proof scores the painted-in figure; below the threshold, or
with proof down, the draft is held. Stub proof clients only."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image

pytest.importorskip("better_profanity")

from argus_mint.cards import IdentityCheck, sha256_hex  # noqa: E402
from argus_mint.identity_check import PROOF_MAJOR, check_identity  # noqa: E402
from argus_mint.moderation import AUTO_OFF, AUTO_REVIEWER, WordFilter, auto_review  # noqa: E402
from argus_mint.projects import Identity, Scene, load_bundle  # noqa: E402
from argus_mint.server import Settings, create_app  # noqa: E402
from argus_mint.server_training import TrainingClients  # noqa: E402
from argus_mint.training_clients import ProofClient, TrainingClientError  # noqa: E402
from tests.test_teasers import (  # noqa: E402
    AUTH,
    OWNER,
    PID,
    REPO,
    TOKEN,
    Stub,
    _make,
    approve,
    approve_frame,
    choose,
    png,
    rendered_background,
)

SHA = "ab" * 32
AREA = {"x": 96, "y": 512, "width": 288, "height": 512}  # lamp-room's character_area


class Proof:
    """Stands in for argus-proof's POST /score: answers ``score`` (or raises ``fail``), and keeps each request."""

    def __init__(self, score: float | None = 0.8, *, fail: TrainingClientError | None = None, **over):
        self.value, self.fail, self.over = score, fail, over
        self.asked: list[tuple[list[tuple[str, bytes]], dict]] = []

    def score(self, images, request):
        self.asked.append((images, request))
        if self.fail is not None:
            raise self.fail
        metric = request["scorers"][0]
        return {
            "proof_version": f"{PROOF_MAJOR}.9",
            "n_images": 1,
            "images": [{"index": 0, "filename": "art.png", "crop": request["crops"][0], "metrics": {metric: self.value}}],
            "means": {metric: self.value},
            "scorers": [
                {"name": "held-out-references", "metric": "held_out", "version": None, "model": None, "model_file": None},
                {"name": "dinov2" if metric == "likeness" else "arcface", "metric": metric, "version": "0.2.0",
                 "model": "facebook/dinov2-base", "model_file": {"name": "dinov2-base.safetensors", "sha256": SHA}},
            ],
        } | self.over  # fmt: skip


def calibrate(root: Path, *, min_score: float | None = 0.6, metric: str = "likeness") -> None:
    """Give the keeper a LoRA that passed proof (its export of references) and, unless None, a threshold."""
    f = root / "identities" / "keeper-v1.yaml"
    doc = yaml.safe_load(f.read_text())
    doc |= {"lora": {"name": "keeper.safetensors"}, "proof_run": "keeper-1", "proof_export": "keeper-refs",
            "check_metric": metric, "min_score": min_score}  # fmt: skip
    f.write_text(yaml.safe_dump(doc, sort_keys=False))


@pytest.fixture
def world(tmp_path):
    import shutil

    from argus_mint.backgrounds import BackgroundStore

    root = tmp_path / "projects" / PID
    shutil.copytree(REPO / "projects" / PID, root)
    calibrate(root)
    approve(root)
    frames = tmp_path / "frames"
    frames.mkdir()
    approve_frame(frames)
    return root, frames, BackgroundStore(tmp_path / "backgrounds")


def parts(root: Path, identity: str = "keeper", scene: str = "lamp-room") -> tuple[Identity, Scene]:
    b = load_bundle(root)
    return b.identities[(identity, 1)][0], b.scenes[(scene, 1)][0]


def art_for(scene: Scene) -> bytes:
    return png(scene.width, scene.height, (10, 20, 30))


# -- the check --------------------------------------------------------------------------------------


def test_proof_scores_the_unframed_art_cropped_to_the_character_area(world):
    root, _frames, _store = world
    identity, scene = parts(root)
    art = art_for(scene)
    proof = Proof(0.71)
    c = check_identity(art=art, identity=identity, scene=scene, proof=proof)
    ((images, request),) = proof.asked
    assert images == [("art.png", art)]
    assert request == {"export": "keeper-refs", "scorers": ["likeness"], "target_category": "identity", "crops": [AREA]}
    assert c.outcome == "passed" and not c.held and c.score == 0.71 and c.min_score == 0.6 and c.metric == "likeness"
    assert c.art_sha256 == sha256_hex(art) and c.crop is not None and c.crop.model_dump() == AREA
    assert c.proof_version == "1.9" and c.scorer == "dinov2" and c.scorer_version == "0.2.0"
    assert c.model_file is not None and c.model_file.sha256 == SHA and c.export == "keeper-refs"
    assert IdentityCheck.model_validate_json(c.model_dump_json()) == c


def test_the_crop_is_the_character_area_in_the_arts_own_pixels(world):
    """character_area is in the scene's render pixels; the art made from it is that size, so it names the figure."""
    root, frames, store = world
    bg, _ = rendered_background(store, root, "lamp-room")
    choose(root, "T1", bg.id)
    made = _make(root, frames, store, renderer=Stub())
    identity, scene = parts(root)
    with Image.open(io.BytesIO(made.art)) as im:
        assert im.size == (scene.width, scene.height)
        a = scene.character_area
        assert im.convert("RGB").getpixel((a.x, a.y)) == (200, 180, 20)  # the stub paints exactly the mask's area
        assert im.convert("RGB").getpixel((a.x - 1, a.y - 1)) != (200, 180, 20)
    assert check_identity(art=made.art, identity=identity, scene=scene, proof=Proof()).outcome == "passed"

    other = png(scene.width // 2, scene.height // 2, (1, 2, 3))  # darkroom answered at another size
    c = check_identity(art=other, identity=identity, scene=scene, proof=(p := Proof()))
    assert c.outcome == "error" and c.held and "512x512" in c.reason and p.asked == []

    rotated = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (scene.width, scene.height)).save(rotated, format="PNG", exif=exif)
    c = check_identity(art=rotated.getvalue(), identity=identity, scene=scene, proof=(p := Proof()))
    assert c.outcome == "error" and "orientation" in c.reason and p.asked == []

    moved = Proof(images=[{"index": 0, "crop": {**AREA, "x": 0}, "metrics": {"likeness": 0.9}}])
    c = check_identity(art=made.art, identity=identity, scene=scene, proof=moved)
    assert c.outcome == "error" and "crop" in c.reason


@pytest.mark.parametrize(
    ("min_score", "score", "outcome", "held"),
    [(0.6, 0.6, "passed", False), (0.6, 0.59, "below", True), (None, 0.42, "uncalibrated", False)],
)
def test_the_verdict_is_the_identitys_threshold(world, min_score, score, outcome, held):
    root, _frames, _store = world
    calibrate(root, min_score=min_score)
    approve(root)
    identity, scene = parts(root)
    c = check_identity(art=art_for(scene), identity=identity, scene=scene, proof=Proof(score))
    assert (c.outcome, c.held, c.score, c.min_score) == (outcome, held, score, min_score)
    assert f"{score:.3f}" in c.reason
    if outcome == "uncalibrated":
        assert "no min_score" in c.reason


def test_a_viewpoint_or_an_unexported_identity_skips_and_says_why(world, tmp_path):
    root, _frames, _store = world
    lamp, shore = parts(root, "lamp", "shoreline")
    c = check_identity(art=png(8, 8, (0, 0, 0)), identity=lamp, scene=shore, proof=(p := Proof()))
    assert c.outcome == "skipped" and not c.held and "viewpoint" in c.reason and p.asked == []

    plain = tmp_path / "plain"
    import shutil

    shutil.copytree(REPO / "projects" / PID, plain)
    approve(plain)
    keeper, scene = parts(plain)
    assert keeper.proof_export is None
    c = check_identity(art=art_for(scene), identity=keeper, scene=scene, proof=(p := Proof()))
    assert c.outcome == "skipped" and c.held and "proof_export" in c.reason and p.asked == []


@pytest.mark.parametrize(
    ("fail", "outcome"),
    [
        (TrainingClientError("proof: connection refused"), "unavailable"),
        (TrainingClientError("proof POST /score: 503 GPU lease refused", 503), "unavailable"),
        (TrainingClientError("proof POST /score: 404 no export", 404), "error"),
        (TrainingClientError("proof POST /score: 400 references overlap", 400), "error"),
    ],
)
def test_proof_down_or_refusing_holds_the_teaser(world, fail, outcome):
    root, _frames, _store = world
    identity, scene = parts(root)
    c = check_identity(art=art_for(scene), identity=identity, scene=scene, proof=Proof(fail=fail))
    assert c.outcome == outcome and c.held and c.score is None and str(fail) in c.reason
    none = check_identity(art=art_for(scene), identity=identity, scene=scene, proof=None)
    assert none.outcome == "unavailable" and none.held and "ARGUS_MINT_PROOF_URL" in none.reason


def test_an_answer_mint_cant_read_holds_the_teaser(world):
    root, _frames, _store = world
    identity, scene = parts(root)
    art = art_for(scene)
    for proof, words in [
        (Proof(None), "no likeness score"),
        (Proof(proof_version="2.0"), "wire '2.0'"),
        (Proof(images=[]), "one image"),
    ]:
        c = check_identity(art=art, identity=identity, scene=scene, proof=proof)
        assert c.outcome == "error" and c.held and words in c.reason, c.reason


# -- auto-moderation --------------------------------------------------------------------------------


def _check(art: bytes, outcome: str, presence: str = "embodied", score: float | None = 0.8) -> IdentityCheck:
    return IdentityCheck(at=datetime.now(UTC), art_sha256=sha256_hex(art), presence=presence, outcome=outcome,
                         reason=f"{outcome} in words", metric="likeness", score=score, min_score=0.6)  # fmt: skip


@pytest.fixture
def teaser(world):
    root, frames, store = world
    bg, _ = rendered_background(store, root, "lamp-room")
    choose(root, "T1", bg.id)
    return _make(root, frames, store, renderer=Stub())


def test_a_passing_identity_score_lets_auto_moderation_approve_and_records_it(teaser):
    words = WordFilter()
    d = auto_review(teaser.draft, teaser.image, words, identity=_check(teaser.art, "passed"))
    assert d.awaiting() == ["owner"] and d.reviews[0].by == AUTO_REVIEWER  # the owner always reviews
    assert d.auto_check is not None and d.auto_check.passed and d.auto_check.identity is not None
    assert "identity: passed in words" in d.auto_check.checks


def test_must_never_an_embodied_teaser_passes_auto_moderation_without_a_passing_identity_score(teaser):
    """Every outcome but ``passed``, no check at all, or a check of other art: held, and no approval."""
    words = WordFilter()
    cases: list[IdentityCheck | None] = [None, _check(b"other art", "passed")]
    cases += [_check(teaser.art, o) for o in ("below", "uncalibrated", "skipped", "unavailable", "error")]
    cases += [_check(teaser.art, "skipped", presence="viewpoint")]  # a viewpoint's skip, for an embodied teaser
    for identity in cases:
        d = auto_review(teaser.draft, teaser.image, words, identity=identity)
        assert d.reviews == () and d.awaiting() == ["moderator", "owner"], identity
        assert d.auto_check is not None and not d.auto_check.passed and d.auto_check.held, identity


def test_with_auto_moderation_off_the_checks_are_recorded_but_never_approve(teaser):
    words = WordFilter()
    for outcome in ("passed", "uncalibrated"):
        d = auto_review(teaser.draft, teaser.image, words, identity=_check(teaser.art, outcome), approve=False)
        assert d.reviews == () and d.auto_check is not None and not d.auto_check.passed
        assert d.auto_check.held == (AUTO_OFF,)  # an uncalibrated score is only recorded, not held on
        assert f"identity: {outcome} in words" in d.auto_check.checks
    d = auto_review(teaser.draft, teaser.image, words, identity=_check(teaser.art, "below"), approve=False)
    assert d.auto_check is not None and d.auto_check.held == ("identity: below in words", AUTO_OFF)


# -- over HTTP --------------------------------------------------------------------------------------


@pytest.fixture
def serve(tmp_path, world):
    root, frames, store = world
    bg, _ = rendered_background(store, root, "lamp-room")
    choose(root, "T1", bg.id)
    shore, _ = rendered_background(store, root, "shoreline")
    choose(root, "T2", shore.id)

    def build(proof, **over):
        cfg = Settings(**({
            "drafts_dir": tmp_path / "drafts", "samples_dir": tmp_path / "samples",
            "templates_dir": REPO / "templates", "frames_dir": frames, "bios_dir": REPO / "bios",
            "projects_dir": root.parent, "backgrounds_dir": tmp_path / "backgrounds", "moderator_token": TOKEN,
            "siwe_domain": "localhost:3108",
        } | over))  # fmt: skip
        app = create_app(cfg, darkroom=Stub(), probe_fn=lambda url: False, training=TrainingClients(proof=proof))
        return TestClient(app)

    return build


def test_must_never_proof_down_lets_a_teaser_through(serve):
    """proof unreachable: the teaser is held with that reason; the moderator reruns the check once it's back."""
    proof = Proof(fail=TrainingClientError("proof: connection refused"))
    c = serve(proof, auto_moderate=True)
    assert c.get("/health").json()["capabilities"]["identity_check"] is True
    r = c.post(f"/projects/{PID}/teasers/T1/draft", json={"to": OWNER}, headers=AUTH)
    assert r.status_code == 201, r.text
    d = r.json()
    auto = d["draft"]["auto_check"]
    assert d["awaiting"] == ["moderator", "owner"] and d["draft"]["reviews"] == []
    assert auto["passed"] is False and auto["identity"]["outcome"] == "unavailable"
    assert any("connection refused" in h for h in auto["held"])
    assert auto["identity"]["crop"] == AREA and auto["identity"]["export"] == "keeper-refs"

    url = f"/drafts/{d['id']}/identity-check"
    assert c.post(url).status_code == 401
    assert c.post(url, headers=AUTH).json()["draft"]["reviews"] == []  # still down: still held
    proof.fail = None  # proof is back
    r = c.post(url, headers=AUTH)
    assert r.status_code == 200, r.text
    again = r.json()
    assert again["awaiting"] == ["owner"] and again["draft"]["reviews"][0]["by"] == AUTO_REVIEWER
    assert again["draft"]["auto_check"]["identity"]["outcome"] == "passed"
    assert again["draft"]["auto_check"]["identity"]["model_file"] == {"name": "dinov2-base.safetensors", "sha256": SHA}
    assert c.post(url, headers=AUTH).status_code == 200  # still pending for the owner: it can run again
    assert c.post("/drafts/nope/identity-check", headers=AUTH).status_code == 404


def test_without_auto_moderation_the_check_is_recorded_and_the_moderator_decides(serve):
    c = serve(Proof(0.3))
    r = c.post(f"/projects/{PID}/teasers/T1/draft", json={"to": OWNER}, headers=AUTH)
    auto = r.json()["draft"]["auto_check"]
    assert auto["identity"]["outcome"] == "below" and auto["held"][-1] == AUTO_OFF
    assert any("under" in h for h in auto["held"])
    did = r.json()["id"]
    r = c.post(f"/drafts/{did}/review", json={"role": "moderator", "approve": False, "by": "m"}, headers=AUTH)
    assert r.json()["status"] == "rejected"
    assert c.post(f"/drafts/{did}/identity-check", headers=AUTH).status_code == 409  # decided: nothing to rerun


def test_a_viewpoint_teaser_isnt_scored_and_no_proof_means_unchecked(serve):
    proof = Proof()
    c = serve(proof, auto_moderate=True)
    r = c.post(f"/projects/{PID}/teasers/T2/draft", json={"to": OWNER}, headers=AUTH)
    auto = r.json()["draft"]["auto_check"]
    assert proof.asked == [] and auto["identity"]["outcome"] == "skipped" and auto["passed"] is True
    assert r.json()["awaiting"] == ["owner"]

    none = serve(None, auto_moderate=True)
    assert none.get("/health").json()["capabilities"]["identity_check"] is False
    r = none.post(f"/projects/{PID}/teasers/T1/draft", json={"to": OWNER}, headers=AUTH)
    assert r.json()["draft"]["auto_check"]["identity"]["outcome"] == "unavailable"
    assert r.json()["awaiting"] == ["moderator", "owner"]


def test_proof_client_sends_the_images_and_the_request_part():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if b"down" in request.read():
            return httpx.Response(503, json={"detail": "GPU lease refused"})
        return httpx.Response(200, json={"proof_version": "1.9", "images": []})

    client = ProofClient("http://proof:8104/", httpx.Client(transport=httpx.MockTransport(handler)))
    body = {"export": "keeper-refs", "scorers": ["likeness"], "target_category": "identity", "crops": [AREA]}
    assert client.score([("art.png", b"\x89PNG")], body)["proof_version"] == "1.9"
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/score"
    raw = req.read()
    assert b'name="images"; filename="art.png"' in raw and b'name="request"' in raw
    assert json.dumps(body).encode() in raw
    with pytest.raises(TrainingClientError) as e:
        client.score([("art.png", b"down")], body)
    assert e.value.status == 503
