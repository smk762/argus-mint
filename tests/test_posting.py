"""Posting approved teasers, the feed a poster reads, publishing a teaser, anchoring and exporting the record."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from argus_mint import merkle
from argus_mint.cards import CardDraft, DraftStore
from argus_mint.metadata import Provenance, TeaserProvenance, canonical, content_hash, teaser_asset_id
from argus_mint.pin import LocalPinner
from argus_mint.posting import (
    AnchorLeaf,
    AnchorRecord,
    AnchorStore,
    anchor_leaves,
    export_teasers,
    leaf_value,
    teaser_drafts,
)
from argus_mint.server import Settings, create_app
from argus_mint.vouchers import Domain, MintVoucher, recover
from argus_mint.workflow import publish
from tests.test_teasers import (
    AUTH,
    OWNER,
    OWNER_KEY,
    PID,
    REPO,
    TOKEN,
    Stub,
    choose,
    rendered_background,
    world,  # noqa: F401 - a fixture
)

POSTER = "herald-poster-token"
POSTER_AUTH = {"Authorization": f"Bearer {POSTER}"}
CARD = "0xCAa3573b5550870A5e15C47c650b8700D70a8fA7"
SIGNER = Account.from_key("0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d")
AT = "2026-11-18T09:30:00Z"


@pytest.fixture
def mint(tmp_path, world):  # noqa: F811
    root, frames, store = world

    def build(**over):
        cfg = Settings(**({
            "drafts_dir": tmp_path / "drafts", "samples_dir": tmp_path / "samples",
            "templates_dir": REPO / "templates", "frames_dir": frames, "bios_dir": REPO / "bios",
            "projects_dir": root.parent, "backgrounds_dir": tmp_path / "backgrounds", "moderator_token": TOKEN,
            "poster_token": POSTER, "siwe_domain": "localhost:3108", "card": CARD,
        } | over))  # fmt: skip
        return TestClient(
            create_app(cfg, darkroom=Stub(), pinner=LocalPinner(), signer=SIGNER, probe_fn=lambda u: False)
        )

    return build, root, store, tmp_path


def teaser(c, root, store, number="T1", *, approve=True, scene="lamp-room", n=1) -> dict:
    """A teaser draft made over HTTP on an approved background; approved by both reviewers unless told not to."""
    bg, _ = rendered_background(store, root, scene, n)
    choose(root, number, bg.id)
    r = c.post(f"/projects/{PID}/teasers/{number}/draft", json={"to": OWNER}, headers=AUTH)
    assert r.status_code == 201, r.text
    draft_id = r.json()["id"]
    if approve:
        c.post(f"/drafts/{draft_id}/review", json={"role": "moderator", "approve": True, "by": "m"}, headers=AUTH)
        ch = c.post(f"/drafts/{draft_id}/challenge", json={"approve": True}).json()
        sig = OWNER_KEY.sign_message(encode_defunct(text=ch["message"])).signature.hex()
        r = c.post(f"/drafts/{draft_id}/review", json={"role": "owner", "approve": True, "message": ch["message"],
                                                       "signature": sig})  # fmt: skip
        assert r.json()["status"] == "approved", r.text
    return c.get(f"/drafts/{draft_id}").json()


def post_body(sha: str, **over) -> dict:
    return {"where": "bluesky", "url": "https://bsky.app/profile/x/post/1", "at": AT, "sha256": sha} | over


# -- recording a post -------------------------------------------------------------------------------


def test_a_post_is_recorded_for_an_approved_teaser_by_the_poster_or_the_moderator(mint):
    build, root, store, _ = mint
    c = build()
    assert c.get("/health").json()["capabilities"]["post"] is True
    d = teaser(c, root, store)
    sha, url = d["draft"]["image_sha256"], f"/drafts/{d['id']}/posted"
    assert hashlib.sha256(c.get(f"/drafts/{d['id']}/image").content).hexdigest() == sha

    assert c.post(url, json=post_body(sha)).status_code == 401
    assert c.post(url, json=post_body(sha), headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.post(url, json=post_body(sha), headers=POSTER_AUTH)
    assert r.status_code == 201, r.text
    (p,) = r.json()["posts"]
    assert p["where"] == "bluesky" and p["by"] == "poster" and p["sha256"] == sha and p["at"] == "2026-11-18T09:30:00Z"
    again = c.post(url, json=post_body(sha), headers=POSTER_AUTH)  # a retry is recorded once
    assert again.status_code == 200 and len(again.json()["posts"]) == 1
    r = c.post(url, json=post_body(sha, where="mastodon", url=None), headers=AUTH)
    assert r.status_code == 201 and [x["by"] for x in r.json()["posts"]] == ["poster", "moderator"]
    assert [x["where"] for x in c.get(f"/drafts/{d['id']}").json()["posts"]] == ["bluesky", "mastodon"]

    assert c.post(url, json=post_body(sha, url="javascript:alert(1)"), headers=POSTER_AUTH).status_code == 422
    assert c.post(url, json=post_body(sha, at="2026-11-18T09:30:00"), headers=POSTER_AUTH).status_code == 422
    assert c.post(url, json=post_body(sha, where="Blue Sky"), headers=POSTER_AUTH).status_code == 422
    assert c.post("/drafts/0123456789abcdef/posted", json=post_body(sha), headers=POSTER_AUTH).status_code == 404


def test_a_post_for_another_image_hash_is_refused(mint):
    """Must never: a post recorded for an image hash other than the draft's."""
    build, root, store, _ = mint
    c = build()
    d = teaser(c, root, store)
    url = f"/drafts/{d['id']}/posted"
    stripped = hashlib.sha256(c.get(f"/drafts/{d['id']}/image").content + b"\0").hexdigest()
    for sha in (stripped, "0" * 64, d["draft"]["art_sha256"]):
        r = c.post(url, json=post_body(sha), headers=POSTER_AUTH)
        assert r.status_code == 409 and "isn't this draft's image" in r.json()["detail"]
    assert c.get(f"/drafts/{d['id']}").json()["posts"] == []

    # The image on disk changed since review: refused even with the reviewed hash.
    image = Path(mint[3]) / "drafts" / d["id"] / "card.png"
    image.write_bytes(image.read_bytes() + b"\0")
    r = c.post(url, json=post_body(d["draft"]["image_sha256"]), headers=POSTER_AUTH)
    assert r.status_code == 409 and "differs" in r.json()["detail"]


def test_only_an_approved_teaser_is_posted(mint):
    build, root, store, _ = mint
    c = build()
    pending = teaser(c, root, store, approve=False)
    r = c.post(f"/drafts/{pending['id']}/posted", json=post_body(pending["draft"]["image_sha256"]), headers=POSTER_AUTH)
    assert r.status_code == 409 and "approved" in r.json()["detail"]

    fixture = json.loads(next((REPO / "fixtures" / "api" / "drafts").glob("*.json")).read_text())
    card = CardDraft.model_validate(fixture["draft"])
    DraftStore(mint[3] / "drafts").save("cardcard", card, b"art", b"image")
    r = c.post("/drafts/cardcard/posted", json=post_body(card.image_sha256), headers=POSTER_AUTH)
    assert r.status_code == 409 and "only a teaser" in r.json()["detail"]


def test_the_poster_token_does_nothing_but_post(mint):
    """Must never: the poster token doing anything but post. Every other write answers it as it
    answers no token at all, and the moderator's routes refuse it outright."""
    build, root, store, _ = mint
    c = build()
    d = teaser(c, root, store, approve=False)
    did = d["id"]
    for path, body in [
        (f"/drafts/{did}/review", {"role": "moderator", "approve": True, "by": "herald"}),
        (f"/drafts/{did}/publish", {}),
        (f"/projects/{PID}/teasers/T1/draft", {"to": OWNER}),
        (f"/projects/{PID}/scenes/lamp-room/1/backgrounds", None),
        (f"/projects/{PID}/backgrounds/lamp-room-v1-01/review", {"decision": "approve", "by": "herald"}),
        ("/templates/example-portrait/3/review", {"decision": "approve", "by": "herald"}),
        (f"/projects/{PID}/files/line/new-line/1", {"doc": {"text": "x"}}),
        ("/samples", {"template": {"id": "example-portrait", "version": 3}}),
    ]:
        r = c.post(path, json=body, headers=POSTER_AUTH)
        assert r.status_code == 401, (path, r.status_code, r.text)
    assert c.get(f"/drafts/{did}").json()["awaiting"] == ["moderator", "owner"]

    params = {"draft_id": did, "pid": PID, "item_id": "x", "version": "1", "scene": "lamp-room", "bg_id": "x",
              "number": "T1", "kind": "line", "item": "x", "frame_id": "x", "job_id": "x", "iid": "keeper",
              "step": "candidates"}  # fmt: skip
    posting = {"/drafts/{draft_id}/posted"}
    for route in c.app.routes:  # type: ignore[attr-defined]
        if "POST" not in getattr(route, "methods", set()) or route.path in posting:
            continue
        path = route.path_format.format(**params)  # convertors (`{frame_id:frame_id}`) left out
        with_poster, without = c.post(path, json={}, headers=POSTER_AUTH), c.post(path, json={})
        assert with_poster.status_code == without.status_code, route.path


def test_the_poster_token_must_differ_and_alone_makes_post_capable(mint, tmp_path):
    build, *_ = mint
    with pytest.raises(ValueError, match="must differ"):
        build(poster_token=TOKEN)
    caps = build(moderator_token="").get("/health").json()["capabilities"]
    assert caps["post"] is True and caps["moderate"] is False
    assert build(moderator_token="", poster_token="").get("/health").json()["capabilities"]["post"] is False
    assert build(read_only=True).get("/health").json()["capabilities"]["post"] is False
    assert Settings.from_env({"ARGUS_MINT_POSTER_TOKEN": "p"}).poster_token == "p"


# -- the feed ---------------------------------------------------------------------------------------


def test_ready_lists_approved_teasers_newest_first_and_marks_the_posted_ones(mint):
    build, root, store, _ = mint
    c = build()
    assert c.get("/posts/ready").json() == {"items": [], "total": 0}
    first = teaser(c, root, store, "T1")
    teaser(c, root, store, "T2", approve=False, scene="shoreline")  # pending: not listed
    second = teaser(c, root, store, "T2", scene="shoreline", n=2)

    ready = c.get("/posts/ready").json()
    assert ready["total"] == 2 and [x["id"] for x in ready["items"]] == [second["id"], first["id"]]
    item = ready["items"][1]
    t1 = first["draft"]
    assert item == {
        "id": first["id"], "project": PID, "title": t1["name"].removesuffix(" T1"), "number": "T1",
        "status": "approved", "current": True, "created_at": item["created_at"], "approved_at": item["approved_at"],
        "image_url": f"/drafts/{first['id']}/image", "image_sha256": t1["image_sha256"],
        "alt": t1["teaser"]["alt"], "quote": t1["texts"]["quote"], "attribution": t1["texts"].get("attribution"),
        "label": t1["texts"]["label"], "posted": False, "posts": [],
    }  # fmt: skip
    assert item["approved_at"] >= item["created_at"]
    image = c.get(item["image_url"]).content
    assert hashlib.sha256(image).hexdigest() == item["image_sha256"]

    c.post(f"/drafts/{first['id']}/posted", json=post_body(t1["image_sha256"]), headers=POSTER_AUTH)
    ready = c.get("/posts/ready").json()["items"]
    assert [(x["id"], x["posted"], len(x["posts"])) for x in ready] == [
        (second["id"], False, 0),
        (first["id"], True, 1),
    ]
    assert c.get("/posts/ready", params={"limit": 1, "offset": 1}).json()["items"][0]["id"] == first["id"]
    assert c.get("/posts/ready", params={"project": "nope"}).json()["total"] == 0
    assert c.get("/posts/ready", params={"project": "../x"}).status_code == 422

    links = root / "teasers" / f"{PID}-v1.json"  # T1 now names another line: the old draft isn't current
    doc = json.loads(links.read_text())
    doc["teasers"]["T1"]["line"] = {"id": "nope", "version": 1}
    links.write_text(json.dumps(doc))
    assert [x["current"] for x in c.get("/posts/ready").json()["items"]] == [True, False]


def test_the_tracker_fills_posted_and_minted_from_the_record(mint):
    build, root, store, _ = mint
    c = build()
    d = teaser(c, root, store)

    def stages():
        return c.get(f"/projects/{PID}/tracker").json()["teasers"][0]["stages"]

    assert stages()["image_made"] is True and stages()["posted"] is False and stages()["minted"] is False
    c.post(f"/drafts/{d['id']}/posted", json=post_body(d["draft"]["image_sha256"]), headers=POSTER_AUTH)
    assert stages()["posted"] is True and stages()["minted"] is False
    r = c.post(f"/drafts/{d['id']}/publish", json={}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert stages()["minted"] is True
    assert c.get("/posts/ready").json()["items"][0]["status"] == "published"


# -- publishing (optional, for either kind) ---------------------------------------------------------


def test_a_teaser_publishes_with_teaser_provenance_and_a_derived_asset_id(mint):
    build, root, store, _ = mint
    c = build()
    d = teaser(c, root, store)
    r = c.post(f"/drafts/{d['id']}/publish", json={}, headers=AUTH)
    assert r.status_code == 200, r.text
    pub = r.json()["published"]
    doc, t = pub["metadata"], d["draft"]["teaser"]
    asset = teaser_asset_id(PID, "T1")
    assert asset == _keccak(f"argus-mint/teaser/{PID}/T1".encode())
    assert pub["voucher"]["asset_id"] == "0x" + asset.hex() and pub["voucher"]["to"] == OWNER
    assert pub["voucher"]["content_hash"] == "0x" + content_hash(doc).hex()
    assert doc["name"] == d["draft"]["name"] and doc["description"] == t["alt"] and doc["attributes"] == []
    prov = doc["provenance"]
    assert set(prov) == {
        "kind", "project", "links", "number", "line", "identity", "scene", "presence", "background", "frame", "art",
        "asset_id", "inpaint",
    }  # fmt: skip
    assert prov["kind"] == "teaser" and prov["number"] == "T1" and prov["presence"] == "embodied"
    for k in ("project", "links", "line", "identity", "scene"):
        assert prov[k] == t[k]
    assert prov["background"] == t["background"] and prov["frame"] == d["draft"]["frame"]
    req = d["draft"]["request"]
    assert prov["inpaint"] == {
        "seed": req["seed"], "attempts": len(d["draft"]["attempts"]), "model": req["model"],
        "image_sha256": req["init_image"]["sha256"], "mask_sha256": req["init_image"]["mask_sha256"], "denoise": 1,
    }  # fmt: skip
    assert TeaserProvenance.model_validate(prov).to_json() == prov  # round-trips, so it hashes the same
    domain = Domain(chain_id=pub["typed_data"]["domain"]["chainId"], verifying_contract=CARD)
    sig = bytes.fromhex(pub["signature"].removeprefix("0x"))
    assert recover(MintVoucher.model_validate(pub["voucher"]), domain, sig) == SIGNER.address
    assert c.post(f"/drafts/{d['id']}/publish", json={}, headers=AUTH).status_code == 409  # once


def test_a_viewpoint_teaser_records_no_inpainting(world):  # noqa: F811
    from tests.test_teasers import _make

    root, frames, store = world
    bg, _ = rendered_background(store, root, "shoreline")
    choose(root, "T2", bg.id)
    made = _make(root, frames, store, "T2", renderer=None)
    prov = made.draft.teaser_provenance("bafkreif3iky42eg7i5ipwrs47kxqvk5l4gfq5ucyxja4pmsddchc2mu7dm")
    assert prov.presence == "viewpoint" and prov.inpaint is None and "inpaint" not in prov.to_json()
    with pytest.raises(ValueError, match="records its inpainting"):
        TeaserProvenance.model_validate(prov.model_dump() | {"presence": "embodied"})
    with pytest.raises(ValueError, match="derived from its project and number"):
        TeaserProvenance.model_validate(prov.model_dump() | {"asset_id": "0x" + "11" * 32})


def test_the_teaser_asset_id_is_fixed_by_project_and_number():
    assert teaser_asset_id("p", "T1") == _keccak(b"argus-mint/teaser/p/T1")
    assert teaser_asset_id("p", "T1") != teaser_asset_id("p", "T2") != teaser_asset_id("q", "T2")
    with pytest.raises(ValueError):
        teaser_asset_id("a/b", "T1")


def test_card_metadata_is_byte_identical():
    """Must never: card metadata changing. A card published before teasers could be, republished
    from its draft, gives the same metadata bytes, content hash, voucher and signature."""
    detail = json.loads((Path(__file__).parent / "fixtures" / "published-card" / "detail.json").read_text())
    pub = detail["published"]
    art, image = b"art", b"image"  # the pinned CIDs stand for the bytes; only the hashes must agree
    d = CardDraft.model_validate(detail["draft"])
    sha = hashlib.sha256(image).hexdigest()
    d = d.model_copy(
        update={"art_sha256": hashlib.sha256(art).hexdigest(), "image_sha256": sha,
                "reviews": tuple(r.model_copy(update={"image_sha256": sha}) for r in d.reviews)}
    )  # fmt: skip

    class Recorded(LocalPinner):
        def pin_bytes(self, data: bytes, name: str) -> str:
            return pub["render"]["art_cid"] if data == art else pub["render"]["image_cid"]

    dom = pub["typed_data"]["domain"]
    out = publish(
        d, art, image, pinner=Recorded(), signer=SIGNER, chain_id=dom["chainId"], card=dom["verifyingContract"],
        price=pub["voucher"]["price"], deadline=pub["voucher"]["deadline"],
    )  # fmt: skip
    out = json.loads(json.dumps(out))
    assert canonical(out["metadata"]) == canonical(pub["metadata"])
    assert "kind" not in out["metadata"]["provenance"]
    assert Provenance.model_validate(out["metadata"]["provenance"]).to_json() == pub["metadata"]["provenance"]
    assert out["voucher"] == pub["voucher"] and out["signature"] == pub["signature"]
    assert out["typed_data"] == pub["typed_data"] and out["render"] == pub["render"]


def _keccak(data: bytes) -> bytes:
    from argus_mint._codec import keccak256

    return keccak256(data)


# -- anchoring and export (the chain part is in test_cli_chain) -------------------------------------


def _record(epoch: int, leaves: list[AnchorLeaf], tree_path: Path) -> AnchorRecord:
    tree = merkle.build([leaf_value(x) for x in leaves])
    tree_path.write_text(json.dumps(tree.dump()))
    return AnchorRecord(
        epoch=epoch, root="0x" + tree.root.hex(), count=len(tree), leaves=tuple(leaves), tree=str(tree_path),
        network="anvil", chain_id=31337, ledger=CARD, tx="0x" + "ab" * 32, block=7, sender=SIGNER.address,
        anchored_at=datetime.now(UTC),
    )  # fmt: skip


def test_each_anchor_takes_the_posted_teasers_no_earlier_anchor_holds(mint):
    build, root, store, tmp = mint
    c = build()
    t1 = teaser(c, root, store, "T1")
    t2 = teaser(c, root, store, "T2", scene="shoreline")
    teaser(c, root, store, "T1", approve=False, n=2)
    drafts = DraftStore(tmp / "drafts")
    assert anchor_leaves(teaser_drafts(drafts, PID), ()) == []  # nothing posted

    for d in (t2, t1):
        c.post(f"/drafts/{d['id']}/posted", json=post_body(d["draft"]["image_sha256"]), headers=POSTER_AUTH)
    leaves = anchor_leaves(teaser_drafts(drafts, PID), ())
    assert [(x.number, x.draft, x.sha256) for x in leaves] == [
        ("T1", t1["id"], t1["draft"]["image_sha256"]), ("T2", t2["id"], t2["draft"]["image_sha256"])
    ]  # fmt: skip
    assert leaf_value(leaves[0]) == bytes.fromhex(t1["draft"]["image_sha256"])

    anchors = AnchorStore(tmp / "anchors")
    anchors.add(PID, _record(1, leaves[:1], tmp / "tree-1.json"))
    assert anchor_leaves(teaser_drafts(drafts, PID), anchors.load(PID)) == leaves[1:]


def test_export_writes_the_reviewed_png_and_its_record(mint):
    from argus_mint.backgrounds import BackgroundStore

    build, root, store, tmp = mint
    c = build()
    d = teaser(c, root, store)
    sha = d["draft"]["image_sha256"]
    c.post(f"/drafts/{d['id']}/posted", json=post_body(sha), headers=POSTER_AUTH)
    drafts = DraftStore(tmp / "drafts")
    anchors = AnchorStore(tmp / "anchors")
    leaves = anchor_leaves(teaser_drafts(drafts, PID), ())
    anchors.add(PID, _record(3, leaves, tmp / "tree-3.json"))

    out = tmp / "export"
    summary = export_teasers(teaser_drafts(drafts, PID), anchors.load(PID), out, BackgroundStore(tmp / "backgrounds"))
    assert summary == [{"number": "T1", "draft": d["id"], "sha256": sha, "posts": 1}]
    png = (out / "T1.png").read_bytes()
    assert png == c.get(f"/drafts/{d['id']}/image").content and hashlib.sha256(png).hexdigest() == sha
    rec = json.loads((out / "T1.json").read_text())
    assert rec["image"] == {"file": "T1.png", "sha256": sha} and rec["draft"] == d["id"]
    assert rec["teaser"] == d["draft"]["teaser"] and rec["frame"] == d["draft"]["frame"]
    req = d["draft"]["request"]
    assert rec["render"]["seed"] == req["seed"] and rec["render"]["checkpoint"] == req["model"]
    assert rec["render"]["loras"] == req["loras"] and rec["render"]["init_image"] == req["init_image"]
    assert rec["background"]["id"] == d["draft"]["teaser"]["background"]["id"] and rec["background"]["checkpoint"]
    assert [p["where"] for p in rec["posts"]] == ["bluesky"] and len(rec["reviews"]) == 2
    (a,) = rec["anchors"]
    assert a["epoch"] == 3 and merkle.verify(bytes.fromhex(a["root"][2:]), bytes.fromhex(sha), a["proof"])


def test_export_cli_and_the_anchor_command_refuse_when_nothing_is_posted(mint):
    from typer.testing import CliRunner

    from argus_mint.cli import app

    build, root, store, tmp = mint
    c = build()
    d = teaser(c, root, store)
    runner = CliRunner()
    args = ["--drafts", str(tmp / "drafts"), "--anchors", str(tmp / "anchors")]
    r = runner.invoke(app, ["teasers", "anchor", PID, "--ledger", CARD, "--epoch", "1", *args])
    assert r.exit_code == 1 and "no posted teaser" in r.output

    r = runner.invoke(app, ["export", PID, "--out", str(tmp / "x"), *args])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["teasers"][0]["draft"] == d["id"]
    assert (tmp / "x" / "T1.png").read_bytes() == c.get(f"/drafts/{d['id']}/image").content
    r = runner.invoke(app, ["export", "nope", "--out", str(tmp / "y"), *args])
    assert r.exit_code == 1 and "no approved teaser" in r.output
