"""Training an identity: candidates, curator, lens, forge, proof and the pin, with stub siblings only."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from argus_mint.projects import load_bundle
from argus_mint.server import Settings, create_app
from argus_mint.server_training import TrainingClients
from argus_mint.training import (
    STEPS,
    StepRun,
    TrainingError,
    TrainingRecord,
    TrainingStore,
    candidate_request,
    candidate_seed,
    check_held_out,
    judge,
    rank_kept,
    refusal,
    split_held_out,
    step_state,
)
from argus_mint.training_clients import CuratorClient, ForgeClient, LensClient, ProofClient
from tests.test_teasers import AUTH, PID, REPO, TOKEN, Stub, approve, png

TRAINING = {
    "model": "sdxl/base.safetensors",
    "sampling": {"sampler": "dpmpp_2m", "scheduler": "karras", "steps": 20, "cfg": 6},
    "width": 512,
    "height": 512,
    "candidates": 12,
    "train": 6,
    "held_out": 3,
    "trigger": "kpr_keeper",
    "proof_seeds": 2,
}
URL = f"/projects/{PID}/identities/keeper/1/training"


# -- stub siblings: each does what the real one does to the shared volume -----------------------------


class FakeCurator:
    """Curator: keeps uploads in a folder, 'scans' them (distance from the file's number), keeps all but
    ``refuse``, and exports a selection flattened into the exports volume with a manifest."""

    def __init__(self, exports: Path, *, refuse: tuple[str, ...] = (), overlap: bool = False):
        self.exports, self.refuse, self.overlap = exports, refuse, overlap
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, Any]] = []

    def upload(self, folder, files):
        self.calls.append(("upload", (folder, [n for n, _ in files])))
        self.files |= dict(files)
        return {"folder": folder, "saved": [n for n, _ in files], "skipped": [], "errors": []}

    def scan(self, body):
        self.calls.append(("scan", body))
        results = [
            {"rel_path": n, "abs_path": f"/data/images/{n}", "score": 0.8,
             "faces": [{"primary": True, "reference_distance": 0.1 + 0.01 * i, "cluster_distance": 0.2}]}
            for i, n in enumerate(sorted(self.files))
        ]  # fmt: skip
        return {"scan_id": "scan-1", "results": results, "reference_skipped": None}

    def select(self, scan_id, body):
        self.calls.append(("select", (scan_id, body)))
        rows = [{"rel_path": n, "abs_path": "", "keep_reason": "face-source:identity" if n in self.refuse else ""}
                for n in sorted(self.files)]  # fmt: skip
        return {"scan_id": scan_id, "total": len(rows), "kept": len(rows), "rows": rows}

    def export(self, body):
        self.calls.append(("export", body))
        dest = self.exports / body["dest"]
        dest.mkdir(parents=True, exist_ok=True)
        selection = list(body["selection"])
        if self.overlap and body["dest"].endswith("/references"):
            train = [c for c in self.calls if c[0] == "export"][0][1]["selection"]
            selection[0] = train[0]  # a curator bug: a training image among the references
        rows = []
        for name in selection:
            (dest / name).write_bytes(self.files[name])
            rows.append({"manifest_version": "2.3", "rel_path": name, "abs_path": f"/data/images/{name}",
                         "exported_path": name, "exported_abs_path": f"/data/out/{body['dest']}/{name}"})  # fmt: skip
        (dest / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        return {"copied": len(rows), "dest": str(dest), "exported_paths": {n: n for n in selection}}


class FakeLens:
    def __init__(self, *, miss: str | None = None):
        self.miss = miss
        self.calls: list[tuple[list[str], dict]] = []

    def caption_batch(self, files, form):
        self.calls.append(([n for n, _ in files], form))
        res = {
            n: {"final_caption": "identity text", "caption_variants": {"identity": "x", "training": "grey beard, coat"}}
            for n, _ in files
            if n != self.miss
        }
        return {"results": res, "errors": {}}


class FakeForge:
    def __init__(self, exports: Path, *, enabled: bool = True, end: str = "succeeded", write: bool = True):
        self.exports, self.enabled, self.end, self.write = exports, enabled, end, write
        self.runs: dict[str, dict] = {}
        self.calls: list[tuple[str, Any]] = []
        self.polls = 0

    def health(self):
        return {"status": "ok", "training": "enabled" if self.enabled else "disabled"}

    def config(self, body):
        self.calls.append(("config", body))
        self.output = body["output_name"]
        return {
            "trainer": "kohya",
            "export_dir": body["export_dir"],
            "output_name": body["output_name"],
            "warnings": [],
        }

    def run(self, body):
        self.calls.append(("run", body))
        run_id = f"forge-{len(self.runs) + 1}"
        self.runs[run_id] = {"run_id": run_id, "status": "running", "export_dir": body["export_dir"]}
        return self.runs[run_id]

    def status(self, run_id):
        self.polls += 1
        state = self.runs.get(run_id)
        if state is None:
            return None
        if state["status"] == "running" and self.polls >= 2:
            state["status"] = self.end
            if self.end == "succeeded" and self.write:
                out = self.exports / state["export_dir"] / "forge" / "kohya" / "output"
                out.mkdir(parents=True, exist_ok=True)
                (out / f"{self.output}.safetensors").write_bytes(b"lora weights " + run_id.encode())
        return state


class FakeProof:
    def __init__(self, loras: Path, *, passed=True, pending=False, identity: float | None = 0.72, other_lora=False):
        self.loras, self.passed, self.pending, self.identity, self.other_lora = (
            loras,
            passed,
            pending,
            identity,
            other_lora,
        )
        self.bodies: list[dict] = []
        self.reports: dict[str, dict] = {}

    def run(self, body):
        self.bodies.append(body)
        self.reports[body["run_id"]] = body
        return {"run_id": body["run_id"], "type": "complete", "report": {"run_id": body["run_id"]}}

    def report(self, run_id):
        if run_id not in self.reports:
            return None
        return {
            "run_id": run_id,
            "verdict": {"passed": self.passed, "pending": self.pending, "reasons": []},
            "aggregate": {"means": {"identity": self.identity, "likeness": None}},
        }

    def manifest(self, run_id):
        body = self.reports[run_id]
        sha = hashlib.sha256((self.loras / body["lora"]).read_bytes()).hexdigest()
        if self.other_lora:
            sha = "0" * 64
        return {"run_id": run_id, "loras": [{"name": body["lora"], "sha256": sha, "weight": 1.0}]}


# -- the world ------------------------------------------------------------------------------------------


def keeper_with_training(root: Path, **extra: Any) -> None:
    f = root / "identities" / "keeper-v1.yaml"
    doc = yaml.safe_load(f.read_text())
    doc |= {"check_metric": "identity", "training": TRAINING} | extra
    f.write_text(yaml.safe_dump(doc, sort_keys=False))


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "projects" / PID
    shutil.copytree(REPO / "projects" / PID, root)
    keeper_with_training(root)
    approve(root)
    exports, loras = tmp_path / "exports", tmp_path / "loras"
    exports.mkdir()
    loras.mkdir()
    return tmp_path, root, exports, loras


def make(world, **over):
    tmp_path, root, exports, loras = world
    sib = {
        "curator": FakeCurator(exports), "lens": FakeLens(), "forge": FakeForge(exports), "proof": FakeProof(loras),
    } | {k: over.pop(k) for k in ("curator", "lens", "forge", "proof") if k in over}  # fmt: skip
    darkroom = over.pop("darkroom", Stub())
    clients = TrainingClients(**sib, exports_dir=exports, loras_dir=loras, poll_seconds=0.0, sleep=lambda s: None)
    cfg = Settings(**({
        "drafts_dir": tmp_path / "drafts", "samples_dir": tmp_path / "samples", "templates_dir": REPO / "templates",
        "frames_dir": REPO / "frames", "bios_dir": REPO / "bios", "projects_dir": root.parent,
        "backgrounds_dir": tmp_path / "backgrounds", "training_dir": tmp_path / "training", "moderator_token": TOKEN,
    } | over))  # fmt: skip
    return TestClient(create_app(cfg, darkroom=darkroom, probe_fn=lambda url: False, training=clients)), sib, darkroom


def step(c: TestClient, name: str, *, expect: str = "done") -> dict:
    r = c.post(f"{URL}/{name}", headers=AUTH)
    assert r.status_code == 202, r.text
    for _ in range(500):
        v = c.get(URL).json()
        if v["steps"][name] in ("done", "failed"):
            assert v["steps"][name] == expect, v["record"]["runs"][-1]
            return v
        time.sleep(0.01)
    raise AssertionError(f"step {name} didn't finish")


# -- the whole run ----------------------------------------------------------------------------------------


def test_training_runs_one_step_at_a_time_and_pins_a_draft_version(world):
    tmp_path, root, exports, loras = world
    c, sib, darkroom = make(world)
    assert c.get("/health").json()["capabilities"]["identity_training"] is True
    v = c.get(URL).json()
    assert v["record"] is None and v["next"] == "candidates" and set(v["steps"].values()) == {"not_started"}
    assert c.post(f"{URL}/candidates").status_code == 401
    assert c.post(f"{URL}/select", headers=AUTH).status_code == 409  # candidates first
    assert c.post(f"{URL}/bake", headers=AUTH).status_code == 404

    v = step(c, "candidates")
    rec = v["record"]
    assert len(rec["candidates"]) == 12 and v["next"] == "select"
    keeper = load_bundle(root).identities[("keeper", 1)][0]
    calls = [req for req, _ in darkroom.calls]
    assert {r.prompt for r in calls} == {keeper.prompt} and all(r.loras == () for r in calls)
    assert {r.model for r in calls} == {"sdxl/base.safetensors"}
    assert calls[0].seed == candidate_seed(PID, keeper, 1) and darkroom.render_asks == [("sdxl/base.safetensors", 0)]
    img = c.get(f"{URL}/candidates/3")
    assert hashlib.sha256(img.content).hexdigest() == rec["candidates"][2]["sha256"]
    assert c.post(f"{URL}/candidates", headers=AUTH).status_code == 409  # done: never again

    v = step(c, "select")
    sel = v["record"]["selection"]
    assert len(sel["train"]) == 6 and len(sel["held_out"]) == 3 and not set(sel["train"]) & set(sel["held_out"])
    scan = next(b for k, b in sib["curator"].calls if k == "scan")
    assert scan["faces"]["enabled"] and scan["faces"]["mivolo"]
    assert sel["train_export"] == f"mint-{PID}-keeper-v1" and sel["proof_export"] == f"mint-{PID}-keeper-v1-proof"
    proof_dir = exports / sel["proof_export"]
    assert (proof_dir / "manifest.jsonl").read_text() == (exports / sel["train_export"] / "manifest.jsonl").read_text()
    assert sorted(p.name for p in (proof_dir / "references").glob("*.png")) == sorted(sel["held_out"])

    v = step(c, "caption")
    txt = sorted((exports / sel["train_export"]).glob("*.txt"))
    assert len(txt) == 6 and all(t.read_text() == "kpr_keeper, grey beard, coat\n" for t in txt)
    assert sib["lens"].calls[0][1] == {"target_style": "photo", "target_backend": "sdxl", "trigger_word": ""}
    assert sorted(sib["lens"].calls[0][0]) == sorted(sel["train"])  # never a held-out image

    v = step(c, "train")
    lora = v["record"]["lora"]
    assert lora["name"] == f"mint/mint-{PID}-keeper-v1.safetensors" and (loras / lora["name"]).is_file()
    config = next(b for k, b in sib["forge"].calls if k == "config")
    assert config == {"export_dir": sel["train_export"], "trainer": "kohya", "trigger": "kpr_keeper",
                      "output_name": f"mint-{PID}-keeper-v1", "collect_captions": False, "dry_run": False}  # fmt: skip
    assert v["record"]["runs"][-1]["sibling"] == {"forge_run": "forge-1"}

    v = step(c, "evaluate")
    ev = v["record"]["evaluation"]
    assert ev["passed"] and ev["score"] == 0.72 and ev["metric"] == "identity"
    body = sib["proof"].bodies[0]
    assert body["export"] == sel["proof_export"] and body["lora"] == lora["name"]
    assert body["base_checkpoint"] == "sdxl/base.safetensors" and body["prompt"].startswith("kpr_keeper, ")
    assert len(body["seeds"]) == 2

    before = (root / "identities" / "keeper-v1.yaml").read_text()
    v = step(c, "pin")
    assert v["record"]["pinned"]["identity_version"] == 2 and v["next"] is None
    assert (root / "identities" / "keeper-v1.yaml").read_text() == before  # approved files never change
    b = load_bundle(root)
    v2 = b.identities[("keeper", 2)][0]
    assert v2.status == "draft" and v2.lora is not None and v2.lora.name == lora["name"]
    assert v2.lora.trigger_words == ("kpr_keeper",) and v2.proof_run == ev["proof_run"]
    assert v2.proof_export == sel["proof_export"] and v2.prompt == keeper.prompt and v2.check_metric == "identity"
    assert all(s == "done" for s in v["steps"].values())

    review = f"/projects/{PID}/files/identity/keeper/2/review"
    r = c.post(review, json={"decision": "approve", "by": "mod"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"


def test_a_lora_is_approved_only_as_a_passing_training_run_pinned_it(world):
    tmp_path, root, exports, loras = world
    c, _sib, _ = make(world)
    doc = yaml.safe_load((root / "identities" / "keeper-v1.yaml").read_text())
    for k in ("status", "approved_by", "approved_at", "guard", "version"):
        doc.pop(k, None)
    doc |= {"lora": {"name": "mint/handmade.safetensors"}, "proof_run": "run-x", "proof_export": "e"}
    r = c.post(f"/projects/{PID}/files/identity/keeper/3", json={"doc": doc}, headers=AUTH)
    assert r.status_code == 200, r.text
    r = c.post(f"/projects/{PID}/files/identity/keeper/3/review", json={"decision": "approve", "by": "m"}, headers=AUTH)
    assert r.status_code == 409 and "no passing training run pinned" in r.json()["detail"]


# -- must never happen ------------------------------------------------------------------------------------


def _record(**runs: str) -> TrainingRecord:
    r = TrainingRecord(project=PID, identity="keeper", identity_version=1, identity_hash="0x" + "0" * 64,
                       metric="identity")  # fmt: skip
    out = []
    for i, (s, status) in enumerate(runs.items()):
        out.append(StepRun(step=s, job=f"{i:016x}", status=status, started_at="2026-11-18T00:00:00Z"))  # type: ignore[arg-type]
    return r.model_copy(update={"runs": tuple(out)})


def test_a_step_waits_for_the_one_before():
    assert refusal(None, "candidates", set()) is None
    assert "waits for candidates" in refusal(None, "select", set())  # type: ignore[operator]
    r = _record(candidates="done", select="failed")
    assert refusal(r, "select", set()) is None  # a failed step may run again
    assert "waits for select" in refusal(r, "caption", set())  # type: ignore[operator]
    assert "is done" in refusal(r, "candidates", set())  # type: ignore[operator]
    running = _record(candidates="done", select="running")
    live = {running.runs[-1].job}
    assert "one step at a time" in refusal(running, "select", live)  # type: ignore[operator]
    assert "one step at a time" in refusal(running, "caption", live)  # type: ignore[operator]
    # a restart: the running job isn't this process's, so it reads as failed and may run again
    assert step_state(running, set())["select"] == "failed" and refusal(running, "select", set()) is None
    for i, s in enumerate(STEPS[1:], 1):
        done = _record(**dict.fromkeys(STEPS[: i - 1], "done"))
        assert refusal(done, s, set()) is not None


def test_a_step_waits_for_the_one_before_over_http(world):
    c, _sib, _ = make(world)
    for s in STEPS[1:]:
        r = c.post(f"{URL}/{s}", headers=AUTH)
        assert r.status_code == 409 and "waits for" in r.json()["detail"]
    step(c, "candidates")
    for s in STEPS[2:]:
        assert c.post(f"{URL}/{s}", headers=AUTH).status_code == 409


def test_a_held_out_reference_never_reaches_training(world, tmp_path):
    ranked = [f"c{i:02d}.png" for i in range(12)]
    train, held = split_held_out(ranked, 6, 3)
    assert len(train) == 6 and len(held) == 3 and not set(train) & set(held)
    assert set(train) | set(held) == set(ranked[:9])
    with pytest.raises(TrainingError, match="need more"):
        split_held_out(ranked[:8], 6, 3)

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "x.png").write_bytes(png(8, 8, (1, 2, 3)))
    (b / "y.png").write_bytes(png(8, 8, (1, 2, 3)))  # the same image under another name
    with pytest.raises(TrainingError, match="held-out references are training images"):
        check_held_out(a, b)

    _, _, exports, _ = world
    c, sib, _ = make(world, curator=FakeCurator(exports, overlap=True))
    step(c, "candidates")
    v = step(c, "select", expect="failed")
    assert "held-out references are training images" in v["record"]["runs"][-1]["error"]
    assert v["record"]["selection"] is None
    assert c.post(f"{URL}/caption", headers=AUTH).status_code == 409


def test_pin_needs_a_passing_proof_run(world):
    passing = {"verdict": {"passed": True, "pending": False}, "aggregate": {"means": {"identity": 0.7}}}
    assert judge(passing, "identity", None)[0] is True
    assert judge(passing, "identity", 0.75)[0] is False
    assert judge({**passing, "verdict": {"passed": False}}, "identity", None)[0] is False
    assert judge({**passing, "verdict": {"passed": True, "pending": True}}, "identity", None)[0] is False
    assert judge({**passing, "aggregate": {"means": {"identity": None}}}, "identity", None)[0] is False
    likeness = {"verdict": {"passed": True}, "aggregate": {"means": {"likeness": 0.9}}}
    assert judge(likeness, "likeness", None)[0] is False  # likeness has no weight in proof's verdict
    assert judge(likeness, "likeness", 0.8)[0] is True

    tmp_path, root, exports, loras = world
    proof = FakeProof(loras, pending=True)
    c, _sib, _ = make(world, proof=proof)
    for s in STEPS[:-1]:
        step(c, s)
    v = step(c, "pin", expect="failed")
    assert "doesn't pass" in v["record"]["runs"][-1]["error"] and v["record"]["pinned"] is None
    assert ("keeper", 2) not in load_bundle(root).identities
    proof.pending, proof.other_lora = False, True  # a person rated the borderline images; but another LoRA
    v = step(c, "pin", expect="failed")
    assert "another LoRA" in v["record"]["runs"][-1]["error"]
    proof.other_lora = False
    v = step(c, "pin")
    assert v["record"]["pinned"]["identity_version"] == 2


# -- each step ----------------------------------------------------------------------------------------------


def test_candidates_retry_safety_rejections_and_resume_after_an_error(world):
    darkroom = Stub("rejected", "ok", "error")
    c, _sib, _ = make(world, darkroom=darkroom)
    v = step(c, "candidates", expect="failed")
    rec = v["record"]
    assert [x["outcome"] for x in rec["candidates"][0]["attempts"]] == ["safety_rejected", "ok"]
    assert [f["n"] for f in rec["failed"]] == [2] and rec["failed"][0]["final"] is False
    assert len(rec["candidates"]) == 11
    asked = len(darkroom.calls)
    v = step(c, "candidates")  # resumes: only number 2 is rendered again
    assert len(v["record"]["candidates"]) == 12 and v["record"]["failed"] == []
    assert len(darkroom.calls) == asked + 1
    keeper = load_bundle(world[1]).identities[("keeper", 1)][0]
    assert candidate_request(keeper, 5).prompt == keeper.prompt  # the reviewed text, nothing else
    assert [r["status"] for r in v["record"]["runs"]] == ["failed", "done"]


def test_likeness_identities_stop_at_select(tmp_path):
    root = tmp_path / "projects" / PID
    shutil.copytree(REPO / "projects" / PID, root)
    keeper_with_training(root, check_metric="likeness")
    approve(root)
    (tmp_path / "exports").mkdir()
    (tmp_path / "loras").mkdir()
    c, sib, _ = make((tmp_path, root, tmp_path / "exports", tmp_path / "loras"))
    step(c, "candidates")
    v = step(c, "select", expect="failed")
    assert "curator can select by consistency only for a face" in v["record"]["runs"][-1]["error"]
    assert sib["curator"].calls == []


def test_train_needs_forge_training_and_follows_a_run_already_started(world):
    tmp_path, root, exports, loras = world
    forge = FakeForge(exports, enabled=False)
    c, _sib, _ = make(world, forge=forge)
    for s in STEPS[:3]:
        step(c, s)
    v = step(c, "train", expect="failed")
    assert "trusted GPU host" in v["record"]["runs"][-1]["error"] and forge.calls == []
    forge.enabled, forge.end = True, "failed"
    v = step(c, "train", expect="failed")
    assert v["record"]["runs"][-1]["sibling"] == {"forge_run": "forge-1"}
    forge.end = "succeeded"
    forge.runs["forge-1"]["status"] = "running"  # still training in forge, say, when mint gave up
    forge.polls = 0
    v = step(c, "train")
    assert [k for k, _ in forge.calls].count("run") == 1  # followed, not started twice
    assert v["record"]["lora"]["forge_run"] == "forge-1"


def test_caption_refuses_an_image_lens_left_out(world):
    _, _, exports, _ = world
    c, sib, _ = make(world)
    for s in STEPS[:2]:
        step(c, s)
    sib["lens"].miss = c.get(URL).json()["record"]["selection"]["train"][0]
    v = step(c, "caption", expect="failed")
    assert "lens didn't caption" in v["record"]["runs"][-1]["error"]


def test_refusals_before_anything_runs(world):
    tmp_path, root, exports, loras = world
    c, _sib, _ = make(world)
    lamp = f"/projects/{PID}/identities/lamp/1/training/candidates"
    assert c.post(lamp, headers=AUTH).status_code == 409  # a viewpoint
    assert c.post(f"/projects/{PID}/identities/keeper/9/training/candidates", headers=AUTH).status_code == 404
    unconfigured = TestClient(create_app(
        Settings(projects_dir=root.parent, moderator_token=TOKEN, training_dir=tmp_path / "t2",
                 drafts_dir=tmp_path / "d2"),
        darkroom=Stub(), probe_fn=lambda url: False,
    ))  # fmt: skip
    assert unconfigured.get("/health").json()["capabilities"]["identity_training"] is False
    r = unconfigured.post(f"{URL}/candidates", headers=AUTH)
    assert r.status_code == 503 and "ARGUS_MINT_CURATOR_URL" in r.json()["detail"]
    refused = Stub(render=["no workflow"])
    c2, _, _ = make(world, darkroom=refused, training_dir=tmp_path / "t3")
    assert c2.post(f"{URL}/candidates", headers=AUTH).status_code == 409


def test_the_record_is_kept_and_a_changed_file_is_refused(world):
    tmp_path, root, exports, loras = world
    c, _sib, _ = make(world)
    step(c, "candidates")
    store = TrainingStore(tmp_path / "training")
    r = store.load(PID, "keeper", 1)
    assert r is not None and r.runs[0].inputs["count"] == 12 and r.runs[0].outputs["rendered"] == 12
    keeper = load_bundle(root).identities[("keeper", 1)][0]
    with pytest.raises(TrainingError, match="another file"):
        store.start(PID, keeper.model_copy(update={"prompt": "someone else"}))


def test_ranking_prefers_the_closest_face():
    scan = {"results": [
        {"rel_path": "a.png", "score": 0.9, "faces": [{"primary": True, "reference_distance": 0.3}]},
        {"rel_path": "b.png", "score": 0.5, "faces": [{"primary": True, "reference_distance": 0.1}]},
        {"rel_path": "c.png", "score": 0.7, "faces": [{"primary": True, "cluster_distance": 0.2}]},
        {"rel_path": "d.png", "score": 0.9, "faces": []},
    ]}  # fmt: skip
    assert rank_kept(scan, ["a.png", "b.png", "c.png", "d.png"]) == ["b.png", "c.png", "a.png", "d.png"]


# -- the HTTP clients send what each sibling's server takes ----------------------------------------------


def _mock(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_clients_speak_each_siblings_routes():
    seen: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.read()))
        if request.url.path == "/run/stream":
            body = b'{"run_id":"r","type":"start","total":2}\n{"run_id":"r","type":"complete","report":{}}\n'
            return httpx.Response(200, content=body, headers={"content-type": "application/x-ndjson"})
        if request.url.path == "/run/gone":
            return httpx.Response(404, json={"detail": "no such run"})
        if request.url.path == "/scan/folder":
            return httpx.Response(409, json={"detail": "a scan is already running"})
        return httpx.Response(200, json={"ok": True})

    cur = CuratorClient("http://curator:8101/", client=_mock(handler))
    cur.upload("mint/p/k-v1", [("a.png", b"x")])
    assert seen[-1][:2] == ("POST", "/upload") and b'name="folder"' in seen[-1][2] and b'name="files"' in seen[-1][2]
    with pytest.raises(Exception, match="409 a scan is already running"):
        cur.scan({"folder": "f"})
    cur.select("s1", {"face_source": {}})
    assert seen[-1][:2] == ("POST", "/scan/s1/select")
    cur.export({"scan_id": "s1", "selection": ["a.png"], "dest": "d"})
    assert seen[-1][:2] == ("POST", "/export") and json.loads(seen[-1][2])["selection"] == ["a.png"]

    LensClient("http://lens:8100", client=_mock(handler)).caption_batch([("a.png", b"x")], {"target_style": "photo"})
    assert seen[-1][:2] == ("POST", "/caption/batch") and b'name="target_style"' in seen[-1][2]

    forge = ForgeClient("http://forge:8103", client=_mock(handler))
    forge.health()
    forge.config({"export_dir": "e", "trainer": "kohya"})
    forge.run({"export_dir": "e", "trainer": "kohya"})
    assert [s[:2] for s in seen[-3:]] == [("GET", "/health"), ("POST", "/config"), ("POST", "/run")]
    assert forge.status("gone") is None

    proof = ProofClient("http://proof:8104", client=_mock(handler))
    assert proof.run({"lora": "l", "base_checkpoint": "c"})["type"] == "complete"
    proof.report("r1")
    proof.manifest("r1")
    assert [s[:2] for s in seen[-3:]] == [
        ("POST", "/run/stream"),
        ("GET", "/report/r1"),
        ("GET", "/report/r1/manifest"),
    ]


def test_references_are_checked_again_whenever_training_uses_them(tmp_path):
    """Consent can be revoked after the identity was approved: select and evaluate check it again,
    before any reference reaches curator or proof (and with no consent records here, refuse)."""
    pytest.importorskip("argus_cortex")
    from argus_mint.projects import Identity, Project
    from argus_mint.references import ConsentStore
    from argus_mint.training import TrainingContext, run_step
    from tests.test_references import CONSENTED, IMAGE, RELEASE, _record

    ident = Identity(
        id="jo", version=1, status="approved", approved_by="m", approved_at="2026-11-18", project=PID,
        voice="keeper", presence="embodied", prompt="a keeper",
        references=[{"file": "jo.png", "sha256": IMAGE, "provenance": CONSENTED}],
    )  # fmt: skip
    consent_dir = tmp_path / "consent"
    consent_dir.mkdir()
    revoked = {"requested_on": "2026-01-02", "effective_on": "2026-01-03", "by": "subject"}
    (consent_dir / f"{RELEASE}.json").write_text(json.dumps(_record(revocation=revoked)))
    curator = FakeCurator(tmp_path / "exports")
    for consent, why in ((ConsentStore(consent_dir), "(revoked)"), (None, "no consent records are configured")):
        ctx = TrainingContext(
            store=TrainingStore(tmp_path / "training"), project=Project(id=PID, version=1, status="draft", title="T"),
            identity=ident, identity_dir=tmp_path, exports_dir=tmp_path / "exports", loras_dir=tmp_path / "loras",
            curator=curator, proof=object(), consent=consent,
        )  # fmt: skip
        for step_name in ("select", "evaluate"):
            with pytest.raises(TrainingError, match=re.escape(why)):
                run_step(step_name, ctx, lambda **_: None)
    assert curator.calls == []  # nothing reached curator
