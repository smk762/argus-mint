"""Teaser poses: a reviewed pose file, a teaser link that names one, and the figure painted in it.

The keypoints are made up in code (a line of points across a box): there is no image and no person.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from argus_cortex.hashing import sha256_hex
from pydantic import ValidationError

from argus_mint._codec import canonical
from argus_mint.cards import CardDraft, TeaserRecord
from argus_mint.darkroom import DarkroomClient, DarkroomError, check_inpaint_pose, request_body
from argus_mint.metadata import TeaserProvenance
from argus_mint.projects import (
    POSE_KEYPOINTS,
    GuardVerdict,
    Pose,
    ProjectError,
    TeaserLinks,
    load_bundle,
    load_pose,
    problems,
)
from argus_mint.render import InitImage, RenderFailed
from argus_mint.spoilers import parse
from argus_mint.teasers import INPAINT_DENOISE, TeaserError, inpaint_request, mask_png, pose_use
from argus_mint.templates import RenderRequest
from tests.test_server_projects import _approve, _save
from tests.test_server_projects import make as make  # noqa: PLC0414 - the fixture
from tests.test_teasers import (
    LINKS,
    PID,
    REPO,
    InpaintingDarkroom,
    Stub,
    _make,
    choose,
    png,
    rendered_background,
)
from tests.test_teasers import world as world  # noqa: PLC0414 - the fixture

MODEL = "sdxl/juggernautXL_ragnarokBy/juggernautXL_ragnarokBy.safetensors"


def keypoints(shift: float = 0.0) -> list[list[float]]:
    """133 made-up points: nothing but numbers."""
    return [[100.0 + shift + i * 1.5, 50.0 + i * 3.25, 0.9] for i in range(POSE_KEYPOINTS)]


def pose_doc(**over) -> dict:
    return {
        "id": "arm-raised",
        "version": 1,
        "status": "draft",
        "project": PID,
        "description": "standing, one arm raised",
        "source": "made up for the tests",
        "width": 512,
        "height": 768,
        "keypoints": keypoints(),
    } | over


def write_pose(root: Path, *, approved: bool = True, guard: bool = True, **over) -> Pose:
    """A pose file in the project, approved with a passing verdict on its description unless told otherwise."""
    doc = pose_doc(**over)
    if approved:
        doc |= {"status": "approved", "approved_by": "tester", "approved_at": "2026-11-18"}
    if guard:
        doc["guard"] = [
            GuardVerdict(field="description", sha256=sha256_hex(doc["description"].encode()), policy="default",
                         passed=True, checked_at=datetime.now(UTC)).model_dump(mode="json")
        ]  # fmt: skip
    (root / "poses").mkdir(exist_ok=True)
    path = root / "poses" / f"{doc['id']}-v{doc['version']}.json"
    path.write_text(json.dumps(doc))
    return load_pose(path)


def link_pose(root: Path, number: str = "T1", pose_id: str = "arm-raised", version: int = 1) -> None:
    doc = json.loads((root / LINKS).read_text())
    doc["teasers"][number]["pose"] = {"id": pose_id, "version": version}
    (root / LINKS).write_text(json.dumps(doc))


class PoseStub(Stub):
    """A darkroom stand-in that can paint a figure in a pose."""

    def __init__(self, *outcomes: str, refuses: list[str] | None = None):
        super().__init__(*outcomes)
        self.refuses = refuses
        self.pose_asks: list[tuple[str, int, str | None]] = []

    def check_inpaint_pose(self, model: str, loras: int, controlnet: str | None = None) -> list[str]:
        self.pose_asks.append((model, loras, controlnet))
        return [] if self.refuses is None else self.refuses


@pytest.fixture
def posed(world):
    """The approved example project with an approved pose linked to T1 and a background chosen."""
    root, frames, store = world
    write_pose(root)
    link_pose(root)
    bg, _ = rendered_background(store, root, "lamp-room")
    choose(root, "T1", bg.id)
    return root, frames, store


# -- the file ----------------------------------------------------------------------------------------


def test_a_pose_is_133_finite_points_in_a_frame() -> None:
    pose = Pose.model_validate(pose_doc())
    assert len(pose.keypoints) == 133 and (pose.strength, pose.fit, pose.hands, pose.face) == (
        0.8,
        "skeleton",
        True,
        True,
    )
    assert pose.guarded_texts() == [("description", "standing, one arm raised")]
    for bad in (
        {"keypoints": keypoints()[:132]},
        {"keypoints": [*keypoints()[:132], [1.0, 2.0, float("nan")]]},
        {"keypoints": [*keypoints()[:132], [1.0, 2.0]]},
        {"width": 0},
        {"height": 70000},
        {"strength": 0},
        {"strength": 2.5},
        {"fit": "stretch"},
        {"description": ""},
        {"source_sha256": "not-a-hash"},
        {"image_base64": "aGVsbG8="},  # a pose is numbers: there is no field for a picture
    ):
        with pytest.raises(ValidationError):
            Pose.model_validate(pose_doc(**bad))


def test_a_poses_hash_covers_its_keypoints_and_ignores_review_fields() -> None:
    pose = Pose.model_validate(pose_doc())
    approved = Pose.model_validate(pose_doc(status="approved", approved_by="a", approved_at="2026-11-18"))
    assert pose.hash() != approved.hash()  # approved_by and approved_at are part of the file, as for every kind
    assert pose.hash() == Pose.model_validate(pose_doc(status="draft")).hash()
    moved = Pose.model_validate(pose_doc(keypoints=keypoints(shift=1.0)))
    assert moved.hash() != pose.hash() and moved.keypoints_sha256() != pose.keypoints_sha256()
    assert pose.keypoints_sha256() == hashlib.sha256(canonical(keypoints())).hexdigest()
    assert Pose.model_validate(pose_doc(strength=1.0)).hash() != pose.hash()


def test_a_project_loads_its_poses_and_checks_them_across_files(world) -> None:
    root, _, _ = world
    example = load_bundle(root).poses
    assert list(example) == [("hand-raised", 1)] and example[("hand-raised", 1)][0].status == "draft"
    assert all("pose" not in t for t in json.loads((root / LINKS).read_text())["teasers"].values())  # and unlinked
    write_pose(root)
    b = load_bundle(root)
    assert sorted(b.poses) == [("arm-raised", 1), ("hand-raised", 1)] and problems(b) == []
    link_pose(root, "T1", "missing")
    assert any("T1: no pose missing v1" in p for p in problems(load_bundle(root)))
    link_pose(root, "T1")
    link_pose(root, "T2")  # T2 is a viewpoint: nobody is painted in
    assert any("T2: a pose is for a figure painted into the image" in p for p in problems(load_bundle(root)))
    write_pose(root, id="elsewhere", project="another-project")
    assert any("pose elsewhere belongs to project 'another-project'" in p for p in problems(load_bundle(root)))
    (root / "poses" / "copy.json").write_text((root / "poses" / "arm-raised-v1.json").read_text())
    with pytest.raises(ProjectError, match="duplicate pose arm-raised v1"):
        load_bundle(root)
    (root / "poses" / "broken.json").write_text("{")
    (root / "poses" / "copy.json").unlink()
    with pytest.raises(ProjectError, match="not valid JSON"):
        load_bundle(root)


# -- nothing changes for a teaser without a pose ----------------------------------------------------------


def test_links_requests_and_records_without_a_pose_read_exactly_as_before(world) -> None:
    root, frames, store = world
    raw = json.loads((REPO / "projects" / PID / LINKS).read_text())
    links = TeaserLinks.model_validate(raw)
    assert "pose" not in links.model_dump(mode="json")["teasers"]["T1"]
    # The hash the example links had before poses existed (computed with the code from before this change).
    assert links.hash().hex() == "d924bc9d5dba62d5f04a781c2c6be2eba3388856452b2f745e4a4ab84ada5110"

    bg, _ = rendered_background(store, root, "lamp-room")
    choose(root, "T1", bg.id)
    stub = PoseStub()
    draft = _make(root, frames, store, renderer=stub).draft
    doc = json.loads(draft.model_dump_json())
    assert "pose" not in doc["request"] and "pose" not in doc["teaser"]
    assert stub.pose_asks == [] and stub.calls[0][1].pose is None  # the plain inpainting path
    assert (
        "pose" not in draft.teaser_provenance("bafkreihdwdcefgh4dqkjv67uzcmw7ojee6xedzdetojuzjevtenxquvyku").to_json()
    )
    assert RenderRequest.model_validate(doc["request"]) == draft.request  # and an old draft still loads


# -- the figure is painted in the pose -------------------------------------------------------------------


def test_a_teaser_that_names_a_pose_sends_its_keypoints_and_the_character_area(posed) -> None:
    root, frames, store = posed
    stub = PoseStub()
    made = _make(root, frames, store, renderer=stub)
    b = load_bundle(root)
    pose, scene = b.poses[("arm-raised", 1)][0], b.scenes[("lamp-room", 1)][0]
    ((req, init),) = stub.calls
    assert req.pose == pose_use(pose, scene)
    assert req.pose.area == (scene.character_area.x, scene.character_area.y, scene.character_area.width,
                             scene.character_area.height)  # fmt: skip
    assert (req.pose.keypoints_sha256, req.pose.hash) == (pose.keypoints_sha256(), pose.hash())
    assert init.pose == pose.keypoints and init.pose_sha256() == pose.keypoints_sha256()
    assert stub.pose_asks == [(scene.model, 0, None)]  # asked darkroom whether it can, with a pose

    t = made.draft.teaser
    assert t.pose.id == "arm-raised" and t.pose.hash == "0x" + pose.hash().hex()
    assert CardDraft.model_validate_json(made.draft.model_dump_json()) == made.draft
    again = _make(root, frames, store, renderer=PoseStub())
    assert again.image == made.image and again.draft.request == made.draft.request  # reproducible (hard rule 7)


def test_a_rejection_is_retried_with_the_next_seed_and_the_same_pose(posed) -> None:
    root, frames, store = posed
    stub = PoseStub("rejected", "rejected")
    made = _make(root, frames, store, renderer=stub)
    assert len(stub.calls) == 3 and len({req.seed for req, _ in stub.calls}) == 3
    assert len({req.pose for req, _ in stub.calls}) == 1 and len({init.pose for _, init in stub.calls}) == 1
    assert [a["outcome"] for a in made.draft.attempts] == ["safety_rejected", "safety_rejected", "ok"]
    with pytest.raises(RenderFailed):
        _make(root, frames, store, renderer=PoseStub(*["rejected"] * 8))  # never painted without the pose


def test_only_an_approved_pose_with_a_fresh_verdict_makes_a_teaser(world) -> None:
    root, frames, store = world
    bg, _ = rendered_background(store, root, "lamp-room")
    choose(root, "T1", bg.id)
    link_pose(root)
    write_pose(root, approved=False)
    with pytest.raises(TeaserError, match="pose arm-raised v1 is draft; only approved files make teasers"):
        _make(root, frames, store, renderer=PoseStub())
    write_pose(root, guard=False)
    with pytest.raises(TeaserError, match="pose arm-raised v1: description has no verdict"):
        _make(root, frames, store, renderer=PoseStub())
    write_pose(root, description="standing by the brass lens")
    with pytest.raises(TeaserError, match="held by spoiler rule 1"):
        _make(root, frames, store, renderer=PoseStub(), spoilers=parse("brass lens\n"))


def test_a_darkroom_that_cannot_hold_a_pose_never_paints_the_figure_without_it(posed) -> None:
    root, frames, store = posed
    plain = Stub()  # can inpaint, knows nothing of poses
    with pytest.raises(TeaserError, match="names pose arm-raised, and this renderer can't paint a figure in a pose"):
        _make(root, frames, store, renderer=plain)
    refusing = PoseStub(refuses=["no darkroom inpainting workflow with a pose takes 0 LoRA(s)"])
    with pytest.raises(TeaserError, match="darkroom can't inpaint this teaser: no darkroom inpainting workflow"):
        _make(root, frames, store, renderer=refusing)
    assert plain.calls == [] and refusing.calls == []


def test_a_poses_own_controlnet_is_asked_for(world) -> None:
    root, frames, store = world
    write_pose(root, controlnet="sdxl/openpose.safetensors", strength=1.1, face=False, fit="contain")
    link_pose(root)
    bg, _ = rendered_background(store, root, "lamp-room")
    choose(root, "T1", bg.id)
    stub = PoseStub()
    _make(root, frames, store, renderer=stub)
    req = stub.calls[0][0]
    assert stub.pose_asks[0][2] == "sdxl/openpose.safetensors"
    assert (req.pose.controlnet, req.pose.strength, req.pose.face, req.pose.fit) == (
        "sdxl/openpose.safetensors", 1.1, False, "contain",
    )  # fmt: skip


# -- the darkroom request and its record ----------------------------------------------------------------


def _parts(root: Path = REPO / "projects" / PID):
    b = load_bundle(root)
    scene = b.scenes[("lamp-room", 1)][0]
    pose = Pose.model_validate(pose_doc())
    init = InitImage(png(1024, 1024, (1, 2, 3)), mask_png(1024, 1024, scene.character_area), INPAINT_DENOISE,
                     pose=pose.keypoints)  # fmt: skip
    req = inpaint_request(b.project, b.identities[("keeper", 1)][0], scene, init.ref(), 11, pose_use(pose, scene))
    return req, init, scene


def test_darkroom_gets_the_keypoints_and_never_a_photo() -> None:
    req, init, scene = _parts()
    body = request_body(req, init=init)
    area = scene.character_area
    assert body["pose"] == {
        "keypoints": keypoints(), "width": 512, "height": 768, "strength": 0.8, "hands": True, "face": True,
        "fit": "skeleton", "area": {"x": area.x, "y": area.y, "width": area.width, "height": area.height},
    }  # fmt: skip
    assert "data_base64" not in body["pose"] and "controlnet" not in body["pose"]
    assert "init_image" in body and body["expand"] is False
    other = InitImage(init.image, init.mask, init.denoise, pose=tuple(tuple(p) for p in keypoints(shift=2.0)))
    with pytest.raises(DarkroomError, match="the pose sent must be the one the request records"):
        request_body(req, init=other)
    with pytest.raises(DarkroomError, match="the pose sent must be the one the request records"):
        request_body(req, init=InitImage(init.image, init.mask, init.denoise))  # the request names a pose
    with pytest.raises(DarkroomError, match="the pose sent must be the one the request records"):
        request_body(req.model_copy(update={"pose": None}), init=init)  # keypoints the request doesn't name


class PosingDarkroom(InpaintingDarkroom):
    """Records the pose as darkroom does (``request.pose``), or wrongly when told to."""

    def __init__(self, *outcomes, wrong: str | None = None):
        super().__init__(*outcomes)
        self.wrong = wrong

    def _stream(self, body, outcome):
        resp = super()._stream(body, outcome)
        sent = body.get("pose")
        if sent is None or self.wrong == "ignored":
            if self.wrong == "invented":
                text = resp.text.replace('"workflow_sha256"', '"pose": {"source": "keypoints"}, "workflow_sha256"')
                return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
            return resp
        a = sent["area"]
        record = {
            "source": "photo" if self.wrong == "photo" else "keypoints",
            "width": sent["width"], "height": sent["height"], "strength": sent["strength"],
            "hands": sent["hands"], "face": sent["face"], "fit": sent["fit"],
            "area": [a["x"], a["y"], a["width"], a["height"]],
            "people": [{"keypoints": sent["keypoints"], "primary": True}],
            "controlnet": {"name": "openpose.safetensors", "sha256": "cd" * 32},
            "control": {"sha256": "ef" * 32, "width": 1024, "height": 1024},
        }  # fmt: skip
        if self.wrong == "keypoints":
            record["people"][0]["keypoints"] = keypoints(shift=3.0)
        if self.wrong == "area":
            record["area"] = [0, 0, 1024, 1024]
        if self.wrong == "strength":
            record["strength"] = 0.3
        if self.wrong == "undrawn":
            record["control"] = None
        if self.wrong == "two":
            record["people"].append({"keypoints": keypoints(), "primary": False})
        text = resp.text.replace('"workflow_sha256"', f'"pose": {json.dumps(record)}, "workflow_sha256"')
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})


def _client(fake) -> DarkroomClient:
    return DarkroomClient("http://darkroom:8106", client=httpx.Client(transport=httpx.MockTransport(fake)))


@pytest.mark.parametrize(
    ("wrong", "detail"),
    [
        (None, None),
        ("keypoints", "darkroom drew a different pose"),
        ("two", "darkroom drew a different pose"),
        ("photo", "took the pose from a photo"),
        ("ignored", "darkroom ignored the pose"),
        ("area", "drew the pose in a different area"),
        ("strength", "held the pose at strength 0.3, not 0.8"),
        ("undrawn", "the pose wasn't drawn"),
    ],
)
def test_an_image_is_accepted_only_when_darkrooms_record_names_the_pose_that_was_sent(wrong, detail) -> None:
    req, init, _ = _parts()
    result = _client(PosingDarkroom("passed", wrong=wrong)).generate(req, init=init)
    assert (result.outcome == "ok") is (detail is None)
    if detail is not None:
        assert detail in result.detail


def test_a_pose_nobody_sent_is_an_error() -> None:
    req, init, _ = _parts()
    plain = req.model_copy(update={"pose": None})
    fake = PosingDarkroom("passed", wrong="invented")
    result = _client(fake).generate(plain, init=InitImage(init.image, init.mask, init.denoise))
    assert result.outcome == "error" and "a pose nobody sent" in result.detail
    assert "pose" not in fake.bodies[0]


def test_check_inpaint_pose_reads_darkrooms_pose_workflows_and_controlnets() -> None:
    models = {
        "checkpoints": [{"name": MODEL, "inpaint_lora_counts": [0, 1, 2], "inpaint_pose_lora_counts": [0, 1]}],
        "controlnets": [{"name": "openpose.safetensors"}],
        "default_pose_controlnet": "openpose.safetensors",
    }
    assert check_inpaint_pose(models, MODEL, 1) == []
    assert check_inpaint_pose(models, MODEL, 1, "openpose.safetensors") == []
    assert "with a pose takes 2 LoRA(s)" in check_inpaint_pose(models, MODEL, 2)[0]
    assert "no ControlNet other.safetensors" in check_inpaint_pose(models, MODEL, 0, "other.safetensors")[0]
    assert "no default one" in check_inpaint_pose(models | {"default_pose_controlnet": None}, MODEL, 0)[0]
    assert "no such checkpoint" in check_inpaint_pose(models, "flux/x.safetensors", 0)[0]
    old = {"checkpoints": [{"name": MODEL, "inpaint_lora_counts": [0]}]}  # a darkroom from before pose input
    assert "doesn't say whether it can inpaint with a pose" in check_inpaint_pose(old, MODEL, 0)[0]


# -- provenance -------------------------------------------------------------------------------------------


def test_a_posed_teasers_provenance_names_the_pose_file(posed) -> None:
    root, frames, store = posed
    draft = _make(root, frames, store, renderer=PoseStub()).draft
    prov = draft.teaser_provenance("bafkreihdwdcefgh4dqkjv67uzcmw7ojee6xedzdetojuzjevtenxquvyku")
    pose = load_bundle(root).poses[("arm-raised", 1)][0]
    assert prov.to_json()["pose"] == {"id": "arm-raised", "version": 1, "hash": "0x" + pose.hash().hex()}
    assert TeaserProvenance.model_validate(prov.to_json()) == prov


def test_a_viewpoint_teaser_has_no_pose() -> None:
    f = {"id": "x", "version": 1, "hash": "0x" + "ab" * 32}
    record = {"project": f, "links": f, "number": "T1", "line": f, "identity": f, "scene": f,
              "background": {"id": "bg", "sha256": "cd" * 32}, "alt": "a view"}  # fmt: skip
    TeaserRecord.model_validate(record | {"presence": "embodied", "pose": f})
    with pytest.raises(ValidationError, match="a viewpoint teaser has none"):
        TeaserRecord.model_validate(record | {"presence": "viewpoint", "pose": f})


# -- over HTTP: a pose is authored and reviewed like any project file ------------------------------------------


def _draft_doc() -> dict:
    return {k: v for k, v in pose_doc().items() if k not in ("id", "version", "status")}


def test_a_pose_is_saved_as_a_draft_judged_and_approved(make) -> None:
    c, guard, root = make()
    r = _save(c, "pose", "arm-raised", 1, _draft_doc())
    assert r.status_code == 200, r.text
    assert r.json()["file"]["kind"] == "pose" and r.json()["file"]["status"] == "draft"
    assert (root / "poses" / "arm-raised-v1.json").is_file()
    assert ("standing, one arm raised", "default") in guard.calls  # SingGuard read the description
    listed = c.get(f"/projects/{PID}").json()["files"]
    assert sorted(f["id"] for f in listed if f["kind"] == "pose") == ["arm-raised", "hand-raised"]
    r = _approve(c, "pose", "arm-raised", 1)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert _save(c, "pose", "arm-raised", 1, _draft_doc()).status_code == 409  # approved files never change


def test_a_pose_that_is_not_one_is_refused_and_a_refused_description_holds_approval(make) -> None:
    c, _, root = make()
    assert _save(c, "pose", "short", 1, _draft_doc() | {"keypoints": keypoints()[:10]}).status_code == 422
    assert _save(c, "pose", "pic", 1, _draft_doc() | {"image_base64": "aGVsbG8="}).status_code == 422
    assert [f.name for f in (root / "poses").iterdir()] == ["hand-raised-v1.json"]  # only the example's own
    assert _save(c, "pose", "held", 1, _draft_doc() | {"description": "refuse this"}).status_code == 200
    r = _approve(c, "pose", "held", 1)
    assert r.status_code == 409 and "SingGuard" in r.json()["detail"]


def test_teaser_links_are_approved_only_when_the_pose_they_name_is(make) -> None:
    from tests.test_server_projects import _approve_everything_the_links_name, _links, _off_the_spoiler_list

    c, _, root = make()
    _off_the_spoiler_list(root)
    _approve_everything_the_links_name(c)
    assert _save(c, "pose", "arm-raised", 1, _draft_doc()).status_code == 200
    doc = _links(root)
    doc["teasers"]["T1"]["pose"] = {"id": "arm-raised", "version": 1}
    assert _save(c, "teasers", PID, 2, doc).status_code == 200
    r = _approve(c, "teasers", PID, 2)
    assert r.status_code == 409 and "pose arm-raised v1" in r.json()["detail"]
    assert _approve(c, "pose", "arm-raised", 1).status_code == 200
    assert _approve(c, "teasers", PID, 2).status_code == 200
    stored = json.loads((root / "teasers" / f"{PID}-v2.json").read_text())
    assert (
        stored["teasers"]["T1"]["pose"] == {"id": "arm-raised", "version": 1} and "pose" not in stored["teasers"]["T2"]
    )


# -- importing keypoints from argus-puppet --------------------------------------------------------------------


LIBRARY = {
    "puppet_version": "1.4",
    "age_checked": True,
    "entries": [
        {"id": "a1", "source_image": "a.png", "source_sha256": "ab" * 32, "width": 640, "height": 960,
         "keypoints": keypoints(), "labels": {}, "cluster": 0},
        {"id": "b2", "source_image": "b.png", "source_sha256": "cd" * 32, "width": 512, "height": 512,
         "keypoints": keypoints(shift=9.0), "labels": {}, "cluster": 1},
    ],
}  # fmt: skip
SIDECAR = {
    "puppet_version": "1.4", "image": "photo.jpg", "image_sha256": "ef" * 32, "width": 800, "height": 600,
    "people": [{"keypoints": keypoints(shift=1.0)}, {"keypoints": keypoints(shift=2.0)}], "primary": 1,
}  # fmt: skip


def test_pose_from_puppet_takes_a_library_entry_or_a_results_primary_person() -> None:
    from argus_mint.projects import pose_from_puppet

    got = pose_from_puppet(LIBRARY, "b2")
    assert (got["width"], got["height"], got["source_sha256"]) == (512, 512, "cd" * 32)
    assert (
        got["keypoints"] == keypoints(shift=9.0) and got["source"] == "argus-puppet pose library (age-checked), pose b2"
    )
    assert "not age-checked" in pose_from_puppet(LIBRARY | {"age_checked": False}, "a1")["source"]
    one = pose_from_puppet({"entries": LIBRARY["entries"][:1]})
    assert one["source_sha256"] == "ab" * 32  # a library of one needs no name
    side = pose_from_puppet(SIDECAR)
    assert side["keypoints"] == keypoints(shift=2.0) and side["source_sha256"] == "ef" * 32  # the primary person
    assert side["source"] == "argus-puppet pose result of photo.jpg"
    for doc, entry, why in (
        (LIBRARY, None, "has 2 poses: name one"),
        (LIBRARY, "zz", "has no pose 'zz'"),
        (SIDECAR, "a1", "there are no entries to name"),
        (SIDECAR | {"primary": None}, None, "no primary person"),
        ({"something": "else"}, None, "not an argus-puppet"),
        ([1, 2], None, "not an argus-puppet"),
    ):
        with pytest.raises(ProjectError, match=why):
            pose_from_puppet(doc, entry)


def test_cli_writes_a_draft_pose_that_loads_and_never_overwrites_one(tmp_path) -> None:
    import shutil

    from typer.testing import CliRunner

    from argus_mint.cli import app

    runner = CliRunner()
    root = tmp_path / PID
    shutil.copytree(REPO / "projects" / PID, root)
    library = tmp_path / "poses.library.json"
    library.write_text(json.dumps(LIBRARY))
    args = ["teasers", "pose", str(library), str(root), "--id", "arm-raised", "--entry", "b2",
            "--description", "standing, one arm raised"]  # fmt: skip
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    pose = load_pose(root / "poses" / "arm-raised-v1.json")
    assert pose.status == "draft" and pose.project == PID and pose.source_sha256 == "cd" * 32
    assert json.loads(result.output)["keypoints_sha256"] == pose.keypoints_sha256()
    assert load_bundle(root).poses[("arm-raised", 1)][0] == pose and problems(load_bundle(root)) == []
    again = runner.invoke(app, args)
    assert again.exit_code == 1 and "never changes once written" in again.output
    assert runner.invoke(app, [*args[:5], "next", "--entry", "zz", "--description", "x"]).exit_code == 1
    assert not (root / "poses" / "next-v1.json").exists()
    assert runner.invoke(app, [*args, "--version", "2"]).exit_code == 0
