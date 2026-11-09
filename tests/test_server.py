"""``argus-mint serve``: the draft → review → publish flow over HTTP, and who may do what."""

from __future__ import annotations

import pytest
import yaml
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from argus_mint.pin import LocalPinner
from argus_mint.render import GenResult
from argus_mint.server import Settings, create_app

from .test_frames import ART, _frame

TOKEN = "s3cret-moderator"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SIGNER = Account.from_key("0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d")
CARD = "0x5FbDB2315678afecb367f032d93F642f64180aa3"


class FakeDarkroom:
    def __init__(self):
        self.texts = []

    def generate(self, req):
        return GenResult.ok(ART, ref="rec1")

    def check_text(self, text):
        self.texts.append(text)
        return True, ""


class FakeWriter:
    def write(self, **kw):
        return "{name} polishes a lyrical lute."


@pytest.fixture
def owner():
    return Account.create()


@pytest.fixture
def setup(tmp_path, template_doc):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "t.yaml").write_text(yaml.safe_dump(template_doc))
    fdir = tmp_path / "frames"
    fdir.mkdir()
    _frame(fdir)
    (tmp_path / "bios").mkdir()
    (tmp_path / "bios" / "b.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "test-bio",
                "version": 1,
                "status": "approved",
                "approved_by": "r",
                "approved_at": "2026-11-09",
                "model": "mistral-nemo",
                "system": "Lyrical.",
                "prompt": "About {{name}}, a {role}.",
            }
        )  # fmt: skip
    )

    def make(**over):
        cfg = Settings(
            drafts_dir=tmp_path / "drafts", samples_dir=tmp_path / "samples", templates_dir=tmp_path / "templates", frames_dir=fdir,
            bios_dir=tmp_path / "bios", darkroom_url="http://darkroom", writer_url="http://writer",
            ipfs_api="http://ipfs", network="base-sepolia", card=CARD, moderator_token=TOKEN,
            siwe_domain="assay.example", **over,
        )  # fmt: skip
        app = create_app(
            cfg, darkroom=FakeDarkroom(), writer=FakeWriter(), pinner=LocalPinner(), signer=SIGNER,
            probe_fn=lambda url: True,
        )  # fmt: skip
        return TestClient(app)

    return make


def _create(c, owner, **over):
    body = {
        "template": {"id": "test-portrait", "version": 1},
        "variables": {"role": "ranger", "colour": "none", "pose": "standing"},
        "seed": 7, "asset": "unit-17", "to": owner.address, "name": "Hana",
        "frame": {"id": "test-frame", "version": 1}, "bio": {"id": "test-bio", "version": 1},
    } | over  # fmt: skip
    return c.post("/drafts", json=body, headers=AUTH)


def _owner_review(c, draft_id, wallet, approve=True):
    ch = c.post(f"/drafts/{draft_id}/challenge", json={"approve": approve}).json()
    sig = wallet.sign_message(encode_defunct(text=ch["message"])).signature.hex()
    return c.post(
        f"/drafts/{draft_id}/review",
        json={"role": "owner", "approve": approve, "message": ch["message"], "signature": sig},
    )


def test_health_and_catalogue(setup):
    c = setup()
    h = c.get("/health").json()
    assert c.get("/templates").json()[0]["combinations"] == 8 and c.get("/templates").json()[0]["file"] == "t.yaml"
    assert h["wire_version"] == "1.2" and h["chain_id"] == 84532 and h["card"] == CARD
    assert all(v for k, v in h["capabilities"].items() if k != "auto_moderate")
    assert [t["template"]["id"] for t in c.get("/templates").json()] == ["test-portrait"]
    frames = c.get("/frames").json()
    assert frames[0]["width"] == 300 and set(frames[0]["frame"]["texts"]) == {"name", "bio"}
    assert c.get("/frames/test-frame/1/overlay").headers["content-type"] == "image/png"
    assert c.get("/bios").json()[0]["bio"]["id"] == "test-bio"


def test_full_flow(setup, owner):
    c = setup()
    r = _create(c, owner)
    assert r.status_code == 201, r.text
    d = r.json()
    draft_id = d["id"]
    assert d["status"] == "pending" and d["awaiting"] == ["moderator", "owner"]
    assert d["draft"]["bio"]["text"] == "Hana polishes a lyrical lute."
    assert c.get(f"/drafts/{draft_id}/image").content[:8] == b"\x89PNG\r\n\x1a\n"
    assert c.post(f"/drafts/{draft_id}/publish", json={}, headers=AUTH).status_code == 409  # not reviewed

    r = c.post(f"/drafts/{draft_id}/review", json={"role": "moderator", "approve": True, "by": "mod"}, headers=AUTH)
    assert r.json()["awaiting"] == ["owner"]
    r = _owner_review(c, draft_id, owner)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert r.json()["draft"]["reviews"][1]["by"] == owner.address

    r = c.post(f"/drafts/{draft_id}/publish", json={"deadline": 1900000000}, headers=AUTH)
    assert r.status_code == 200, r.text
    pub = r.json()
    assert pub["status"] == "published" and pub["published"]["signer"] == SIGNER.address
    assert pub["published"]["metadata"]["provenance"]["bio"]["id"] == "test-bio"
    assert c.post(f"/drafts/{draft_id}/publish", json={}, headers=AUTH).status_code == 409  # once
    listing = c.get("/drafts", params={"status": "published"}).json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == draft_id


def test_owner_must_sign_with_the_cards_wallet(setup, owner):
    c = setup()
    draft_id = _create(c, owner).json()["id"]
    stranger = Account.create()
    assert _owner_review(c, draft_id, stranger).status_code == 401  # the challenge names the card's wallet

    ch = c.post(f"/drafts/{draft_id}/challenge", json={"approve": True}).json()
    sig = owner.sign_message(encode_defunct(text=ch["message"])).signature.hex()
    as_reject = {"role": "owner", "approve": False, "message": ch["message"], "signature": sig}
    assert c.post(f"/drafts/{draft_id}/review", json=as_reject).status_code == 401  # signed "approve"
    as_approve = as_reject | {"approve": True}
    assert c.post(f"/drafts/{draft_id}/review", json=as_approve).status_code == 401  # nonce already used
    assert _owner_review(c, draft_id, owner).status_code == 200


def test_tokens_and_roles(setup, owner):
    c = setup()
    assert _create(c, owner).status_code == 201
    r = c.post("/drafts", json={}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code in (401, 422)
    body = {
        "template": {"id": "test-portrait", "version": 1},
        "variables": {"role": "ranger", "colour": "none", "pose": "standing"},
        "seed": 1, "asset": "a", "to": owner.address, "name": "n",
    }  # fmt: skip
    assert c.post("/drafts", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    draft_id = c.get("/drafts").json()["items"][0]["id"]
    no_token = c.post(f"/drafts/{draft_id}/review", json={"role": "moderator", "approve": True, "by": "x"})
    assert no_token.status_code == 401
    owner_without_signature = c.post(f"/drafts/{draft_id}/review", json={"role": "owner", "approve": True})
    assert owner_without_signature.status_code == 422


def test_rejection_and_bad_requests(setup, owner):
    c = setup()
    assert _create(c, owner, template={"id": "nope", "version": 1}).status_code == 404
    assert _create(c, owner, texts={"stats": "3/2"}).status_code == 422  # the test frame has no stats slot
    assert _create(c, owner, variables={"role": "wizard", "colour": "none", "pose": "standing"}).status_code == 422
    assert c.get("/drafts/..%2Fetc").status_code in (400, 404)
    draft_id = _create(c, owner).json()["id"]
    r = c.post(
        f"/drafts/{draft_id}/review",
        json={"role": "moderator", "approve": False, "by": "m", "note": "no"},
        headers=AUTH,
    )
    assert r.json()["status"] == "rejected"
    assert c.post(f"/drafts/{draft_id}/challenge", json={"approve": True}).status_code == 409


def test_cross_site_writes_and_read_only(setup, owner):
    c = setup()
    evil = c.post("/drafts", json={}, headers=AUTH | {"Origin": "https://evil.example"})
    assert evil.status_code == 403
    ro = setup(read_only=True)
    assert ro.get("/health").json()["capabilities"] == {
        "create_drafts": False, "write_bios": False, "moderate": False, "owner_review": False, "publish": False,
        "sample": False, "review_catalogue": False, "edit_text": False, "auto_moderate": False,
    }  # fmt: skip
    assert _create(ro, owner).status_code == 403


def test_settings_from_env():
    s = Settings.from_env({"ARGUS_MINT_CORS_ORIGINS": "https://a, https://b", "ARGUS_MINT_READ_ONLY": "1"})
    assert s.cors_origins == ("https://a", "https://b") and s.read_only is True


def test_wire_schema_is_committed():
    from pathlib import Path

    from argus_cortex.wire import render_schema

    from argus_mint.wire import WIRE_MODELS, WIRE_VERSION

    committed = (Path(__file__).resolve().parents[1] / "schema" / "mint-wire.schema.json").read_text()
    assert committed == render_schema(WIRE_MODELS, title=f"argus-mint wire {WIRE_VERSION}")


def _wait(c, job_id):
    import time

    for _ in range(200):
        job = c.get(f"/samples/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError("sample job did not finish")


def test_samples_render_drafts_to_local_jobs(setup, tmp_path):
    c = setup()
    t = tmp_path / "templates" / "t.yaml"
    t.write_text(t.read_text().replace("status: approved", "status: draft"))  # samples are for drafts
    body = {
        "template": {"id": "test-portrait", "version": 1}, "frame": {"id": "test-frame", "version": 1},
        "bio": {"id": "test-bio", "version": 1}, "count": 3, "variables": {"role": "ranger"}, "name": "Hana",
    }  # fmt: skip
    assert c.post("/samples", json=body).status_code == 401
    r = c.post("/samples", json=body, headers=AUTH)
    assert r.status_code == 202, r.text
    job = _wait(c, r.json()["id"])
    assert job["status"] == "done" and len(job["items"]) == 3
    assert all(i["variables"]["role"] == "ranger" and i["framed"] and i["bio"].startswith("Hana") for i in job["items"])
    assert c.get(f"/samples/{job['id']}/1/framed").content[:4] == b"\x89PNG"
    assert c.get("/samples").json()["total"] == 1
    assert c.post("/samples", json=body | {"variables": {"role": "wizard"}}, headers=AUTH).status_code == 422
    assert c.get("/samples/not-an-id").status_code == 400

    preview = {"art": {"sample": job["id"], "n": 1}, "name": "Hana", "bio": "A lyrical line."}
    r = c.post("/frames/test-frame/1/preview", json=preview)
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert c.post("/frames/test-frame/1/preview", json=preview | {"texts": {"stats": "1"}}).status_code == 422


def test_catalogue_decisions_edit_the_file(setup, tmp_path):
    c = setup()
    t = tmp_path / "templates" / "t.yaml"
    t.write_text(
        "# reviewed by a human\n"
        + t.read_text()
        .replace("status: approved", "status: draft  # draft | approved | retired")
        .replace("approved_by: reviewer\n", "")
        .replace("approved_at: '2026-11-08'\n", "")
    )
    url = "/templates/test-portrait/1/review"
    assert c.post(url, json={"decision": "approve", "by": "mod"}).status_code == 401
    assert c.post(url, json={"decision": "retire", "by": "mod"}, headers=AUTH).status_code == 409  # still a draft
    r = c.post(url, json={"decision": "approve", "by": "dragonhound_dev"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    text = t.read_text()
    assert text.startswith("# reviewed by a human") and "# draft | approved | retired" in text  # comments kept
    assert 'approved_by: "dragonhound_dev"' in text and "approved_at: " in text
    approved_hash = r.json()["hash"]
    r = c.post(url, json={"decision": "retire", "by": "mod"}, headers=AUTH)
    assert r.json()["status"] == "retired" and r.json()["hash"] == approved_hash  # retiring keeps the hash
    assert (
        c.post("/frames/test-frame/1/review", json={"decision": "retire", "by": "m"}, headers=AUTH).json()["status"]
        == "retired"
    )
    assert c.post("/bios/nope/1/review", json={"decision": "approve", "by": "m"}, headers=AUTH).status_code == 404


def _edit_as_owner(c, draft_id, wallet, edit):
    ch = c.post(f"/drafts/{draft_id}/challenge", json={"action": "edit", "edit": edit}).json()
    sig = wallet.sign_message(encode_defunct(text=ch["message"])).signature.hex()
    return c.post(f"/drafts/{draft_id}/text", json=edit | {"role": "owner", "message": ch["message"], "signature": sig})


def test_editing_text_redraws_and_needs_new_approvals(setup, owner):
    c = setup()
    d = _create(c, owner).json()
    draft_id, first_image = d["id"], d["draft"]["image_sha256"]
    c.post(f"/drafts/{draft_id}/review", json={"role": "moderator", "approve": True, "by": "mod"}, headers=AUTH)
    assert _owner_review(c, draft_id, owner).json()["status"] == "approved"

    r = _edit_as_owner(c, draft_id, owner, {"name": "Hana of the Strait", "bio": "A quieter line, by hand."})
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["status"] == "pending" and e["awaiting"] == ["moderator", "owner"]  # a new image: both again
    assert e["draft"]["image_sha256"] != first_image and e["draft"]["name"] == "Hana of the Strait"
    assert e["draft"]["bio"]["edited"] is True and e["draft"]["description"] == "A quieter line, by hand."
    assert e["draft"]["edits"][0]["fields"] == ["name", "bio"] and e["draft"]["edits"][0]["by"] == owner.address
    assert len(e["draft"]["reviews"]) == 2  # history kept
    assert c.get(f"/drafts/{draft_id}/image").content[:4] == b"\x89PNG"

    # an owner signature covers exactly the edit it was issued for
    ch = c.post(f"/drafts/{draft_id}/challenge", json={"action": "edit", "edit": {"name": "A"}}).json()
    sig = owner.sign_message(encode_defunct(text=ch["message"])).signature.hex()
    swapped = {"name": "B", "role": "owner", "message": ch["message"], "signature": sig}
    assert c.post(f"/drafts/{draft_id}/text", json=swapped).status_code == 401

    moderator_edit = {"role": "moderator", "by": "mod", "texts": {"name": "x"}}
    assert c.post(f"/drafts/{draft_id}/text", json=moderator_edit).status_code == 401  # no token
    r = c.post(f"/drafts/{draft_id}/text", json={"role": "moderator", "by": "mod", "name": "Hana"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["draft"]["name"] == "Hana"
    nothing = c.post(f"/drafts/{draft_id}/text", json={"role": "moderator", "by": "mod", "name": "Hana"}, headers=AUTH)
    assert nothing.status_code == 409


def test_auto_moderation_approves_clean_drafts_and_holds_the_rest(setup, owner, tmp_path):
    blocklist = tmp_path / "blocklist.txt"
    blocklist.write_text("# house rules\nzorblax\n")
    c = setup(auto_moderate=True, blocklist=blocklist)
    assert c.get("/health").json()["capabilities"]["auto_moderate"] is True

    d = _create(c, owner).json()
    assert d["awaiting"] == ["owner"]  # the platform approved for the moderator
    auto = d["draft"]["reviews"][0]
    assert auto["by"] == "auto: platform checks" and auto["role"] == "moderator"
    assert d["draft"]["auto_check"]["passed"] is True

    held = _create(c, owner, name="Zorblax the Bold", asset="unit-18").json()
    assert held["awaiting"] == ["moderator", "owner"]
    assert "name" in held["draft"]["auto_check"]["held"][0]

    # a hand-edited bio reached no model, so it goes through the word filter
    r = c.post(f"/drafts/{d['id']}/text", json={"role": "moderator", "by": "m", "bio": "Sh1t happens."}, headers=AUTH)
    e = r.json()
    assert e["awaiting"] == ["moderator", "owner"] and "bio" in e["draft"]["auto_check"]["held"][0]
