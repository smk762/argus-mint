"""Training's provenance edges: reused sibling runs, held-out references, odd scores and clients."""

from __future__ import annotations

import hashlib
import json
import shutil

import httpx
import pytest

from argus_mint.identity_check import check_identity
from argus_mint.training import STEPS, TrainingError, TrainingStore, _forge_lora, judge, split_held_out
from argus_mint.training_clients import ForgeClient, ProofClient, TrainingClientError
from tests.test_identity_check import Proof as ScoreProof
from tests.test_identity_check import art_for, calibrate, parts
from tests.test_teasers import PID, REPO, approve, png
from tests.test_training import FakeCurator, FakeForge, FakeProof, keeper_with_training, make, step

KEY = (PID, "keeper", 1)


def _store(world) -> TrainingStore:
    return TrainingStore(world[0] / "training")


def _edit(world, **fields) -> None:
    store = _store(world)
    with store.lock:
        store.save(store.load(*KEY).model_copy(update=fields))


# -- split and judge ------------------------------------------------------------------------------------


def test_duplicates_in_the_ranking_never_shrink_the_training_set():
    ranked = ["a.png", "a.png", "c.png", "b.png", "d.png", "e.png", "f.png", "g.png", "h.png", "i.png"]
    train, held = split_held_out(ranked, 6, 3)
    assert (len(set(train)), len(train), len(set(held)), len(held), set(train) & set(held)) == (6, 6, 3, 3, set())
    assert set(train) | set(held) == set(ranked)


def test_duplicates_never_count_toward_the_images_needed():
    with pytest.raises(TrainingError, match="kept 8 images"):
        split_held_out(["a.png", "a.png", "c.png", "b.png", "d.png", "e.png", "f.png", "g.png", "h.png"], 6, 3)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True])
def test_only_a_finite_number_is_a_score(bad):
    report = {"verdict": {"passed": True, "pending": False}, "aggregate": {"means": {"identity": bad}}}
    for min_score in (None, 0.5):
        passed, _, _, score, reasons = judge(report, "identity", min_score)
        assert passed is False and score is None
        assert any("didn't score identity" in r for r in reasons)


def test_a_lora_outside_the_export_is_named_in_the_error():
    with pytest.raises(TrainingError, match=r"outside the export \('\.\./evil\.safetensors'\)"):
        _forge_lora(None, "e", "forge-1", "o", {"lora": {"export_path": "../evil.safetensors", "path": "/x"}})


# -- the world, over HTTP ---------------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "projects" / PID
    shutil.copytree(REPO / "projects" / PID, root)
    keeper_with_training(root)
    approve(root)
    (tmp_path / "exports").mkdir()
    (tmp_path / "loras").mkdir()
    return tmp_path, root, tmp_path / "exports", tmp_path / "loras"


def test_a_forge_run_started_for_another_config_is_not_adopted(world):
    _, _, exports, _ = world
    forge = FakeForge(exports, end="failed")
    c, _sib, _ = make(world, forge=forge)
    for s in STEPS[:3]:
        step(c, s)
    step(c, "train", expect="failed")
    r = _store(world).load(*KEY)
    old = r.runs[-1]
    stale = old.model_copy(update={"inputs": {**old.inputs, "trigger": "someone_else"}})
    _edit(world, runs=(*r.runs[:-1], stale))
    forge.end = "succeeded"
    forge.runs["forge-1"]["status"] = "running"  # still going in forge, but trained for another trigger
    forge.polls = 0
    v = step(c, "train")
    assert [k for k, _ in forge.calls].count("run") == 2 and v["record"]["lora"]["forge_run"] == "forge-2"


def test_a_proof_run_is_reused_only_for_the_same_lora(world):
    _, _, _, loras = world

    class SlowProof(FakeProof):  # the report isn't there yet when mint asks for it the first time
        hide = True

        def report(self, run_id):
            return None if self.hide else super().report(run_id)

    proof = SlowProof(loras)
    c, _sib, _ = make(world, proof=proof)
    for s in STEPS[:4]:
        step(c, s)
    step(c, "evaluate", expect="failed")
    assert len(proof.bodies) == 1
    r = _store(world).load(*KEY)  # the LoRA is retrained meanwhile: the old run scored other weights
    (loras / r.lora.name).write_bytes(b"retrained")
    _edit(world, lora=r.lora.model_copy(update={"sha256": hashlib.sha256(b"retrained").hexdigest()}))
    proof.hide = False
    v = step(c, "evaluate")
    assert len(proof.bodies) == 2 and v["record"]["evaluation"]["proof_run"] == proof.bodies[-1]["run_id"]


def test_a_proof_run_for_the_same_lora_is_followed_not_run_again(world):
    _, _, _, loras = world

    class SlowProof(FakeProof):
        hide = True

        def report(self, run_id):
            return None if self.hide else super().report(run_id)

    proof = SlowProof(loras)
    c, _sib, _ = make(world, proof=proof)
    for s in STEPS[:4]:
        step(c, s)
    step(c, "evaluate", expect="failed")
    proof.hide = False
    v = step(c, "evaluate")
    assert len(proof.bodies) == 1 and v["record"]["evaluation"]["proof_run"] == proof.bodies[0]["run_id"]


def test_same_named_candidates_are_refused_before_upload(world):
    tmp_path, _, exports, _ = world
    curator = FakeCurator(exports)
    c, _sib, _ = make(world, curator=curator)
    step(c, "candidates")
    r = _store(world).load(*KEY)
    d = _store(world).dir(*KEY)
    first = r.candidates[0]
    other = f"candidates/again/{first.file.split('/')[-1]}"
    (d / other).parent.mkdir(parents=True)
    shutil.copyfile(d / first.file, d / other)
    _edit(world, candidates=(r.candidates[1].model_copy(update={"file": other}), first, *r.candidates[2:]))
    v = step(c, "select", expect="failed")
    assert "clash by name" in v["record"]["runs"][-1]["error"] and curator.calls == []


def test_a_candidate_rendered_twice_is_recorded_once(world):
    """Two overlapping runs of the step can render the same number: the last one's candidate replaces the first."""
    c, _sib, darkroom = make(world)
    store = _store(world)
    batch = darkroom.batch

    def racing_batch():  # candidates render at batch priority, through darkroom.batch()
        b = batch()
        generate = b.generate

        def racing(req, init=None):  # another run commits candidate 2 while this one renders it
            out = generate(req, init)
            r = store.load(*KEY)
            if len(r.candidates) == 1:
                ghost = r.candidates[0].model_copy(update={"n": 2, "sha256": "00" * 32})
                with store.lock:
                    store.save(r.model_copy(update={"candidates": (*r.candidates, ghost)}))
            return out

        b.generate = racing
        return b

    darkroom.batch = racing_batch
    v = step(c, "candidates")
    ns = [x["n"] for x in v["record"]["candidates"]]
    assert ns == sorted(set(ns)) and len(ns) == 12
    assert v["record"]["candidates"][1]["sha256"] != "00" * 32


def test_a_changed_held_out_reference_never_reaches_proof(tmp_path):
    pytest.importorskip("argus_cortex")
    root = tmp_path / "projects" / PID
    shutil.copytree(REPO / "projects" / PID, root)
    image = png(8, 8, (9, 9, 9))
    (root / "identities" / "held.png").write_bytes(image)
    synthetic = {"kind": "synthetic", "source": "mint", "license": "own",
                 "generator": {"model": "m", "seed": 1, "prompt": "p"}}  # fmt: skip
    ref = {"file": "held.png", "sha256": hashlib.sha256(image).hexdigest(), "provenance": synthetic, "held_out": True}
    keeper_with_training(root, references=[ref])
    approve(root)
    exports, loras = tmp_path / "exports", tmp_path / "loras"
    exports.mkdir()
    loras.mkdir()
    w = (tmp_path, root, exports, loras)
    c, _sib, _ = make(w)
    step(c, "candidates")
    (root / "identities" / "held.png").write_bytes(png(8, 8, (1, 1, 1)))  # swapped after approval
    v = step(c, "select", expect="failed")
    assert "differs from its recorded sha256" in v["record"]["runs"][-1]["error"]
    assert v["record"]["selection"] is None


def test_a_broken_record_is_an_http_error_on_the_candidate_route(world):
    from tests.test_teasers import AUTH

    c, _sib, _ = make(world)
    step(c, "candidates")
    (_store(world).dir(*KEY) / "record.json").write_text("{not json")
    r = c.get(f"/projects/{PID}/identities/keeper/1/training/candidates/1", headers=AUTH)
    assert r.status_code == 500 and "record.json" in r.json()["detail"]


# -- identity check and clients --------------------------------------------------------------------------


def test_an_infinite_identity_score_is_an_error_not_a_pass(tmp_path):
    root = tmp_path / "projects" / PID
    shutil.copytree(REPO / "projects" / PID, root)
    calibrate(root, min_score=0.6)
    approve(root)
    identity, scene = parts(root)
    for bad in (float("inf"), float("nan")):
        ch = check_identity(art=art_for(scene), identity=identity, scene=scene, proof=ScoreProof(bad))
        assert ch.outcome == "error" and "non-finite" in ch.reason and ch.score is None


def test_run_ids_stay_one_path_segment_and_odd_bodies_are_client_errors():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.raw_path.startswith(b"/run/html"):
            return httpx.Response(200, text="<html>proxy</html>")
        if request.url.raw_path.startswith(b"/run/list"):
            return httpx.Response(200, json=[1, 2])
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    proof = ProofClient("http://proof:8104", client)
    proof.report("../score?x=1#y")
    proof.manifest("a/b")
    assert [r.url.raw_path for r in seen] == [b"/report/..%2Fscore%3Fx%3D1%23y", b"/report/a%2Fb/manifest"]
    for bad in ("..", "."):
        with pytest.raises(TrainingClientError):
            proof.report(bad)
    forge = ForgeClient("http://forge:8103", client)
    with pytest.raises(TrainingClientError, match="not JSON"):
        forge.status("html")
    with pytest.raises(TrainingClientError, match="not a JSON object"):
        forge.status("list")
    assert json.loads(json.dumps(forge.status("ok"))) == {"ok": True}
