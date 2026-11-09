from __future__ import annotations

import json

import yaml
from eth_account import Account
from typer.testing import CliRunner

from argus_mint.cli import app
from argus_mint.metadata import content_hash
from argus_mint.vouchers import Domain, MintVoucher, recover

runner = CliRunner()
CARD = "0x5FbDB2315678afecb367f032d93F642f64180aa3"
KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"


OWNER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


def draft_args(path, out, *extra):
    return [
        "draft",
        "--template", str(path),
        "--out", str(out),
        "--var", "role=ranger", "--var", "colour=none", "--var", "pose=standing",
        "--seed", "81234",
        "--asset", "unit-17",
        "--to", OWNER,
        "--name", "Hana of the Strait",
        "--consumer", "atlas-realms",
        *extra,
    ]  # fmt: skip


def publish_args(out, *extra):
    return ["publish", str(out), "--card", CARD, "--chain-id", "31337", "--deadline", "1900000000", *extra]


def approve(out, *roles):
    for role in roles or ("moderator", "owner"):
        result = runner.invoke(app, ["review", str(out), "--as", role, "--by", f"{role}-1", "--approve"])
        assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def make_draft(tmp_path, template_doc, *extra, name="d"):
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(template_doc))
    out = tmp_path / name
    result = runner.invoke(app, draft_args(path, out, *extra))
    assert result.exit_code == 0, result.output
    return out, json.loads(result.stdout)


def test_draft_review_publish_dry_run(tmp_path, template_doc, monkeypatch):
    out, d = make_draft(tmp_path, template_doc, "--placeholder")
    assert d["status"] == "pending" and d["awaiting"] == ["moderator", "owner"]
    assert d["prompt"] == "portrait of a ranger warrior, none heraldry, standing"
    assert (out / "art.png").exists() and not (out / "card.png").exists()  # unframed: the art is the card
    assert approve(out)["status"] == "approved"

    monkeypatch.setenv("ARGUS_MINT_SIGNER_KEY", KEY)
    result = runner.invoke(app, publish_args(out, "--dry-run"))
    assert result.exit_code == 0, result.output
    pub = json.loads(result.stdout)
    assert pub["dry_run"] is True and pub["signer_is_ephemeral"] is False
    assert [r["role"] for r in pub["reviews"]] == ["moderator", "owner"]
    prov = pub["metadata"]["provenance"]
    assert prov["template"] == "test-portrait" and prov["seed"] == 81234
    assert "frame" not in prov and "art" not in prov and pub["render"]["art_cid"] is None
    voucher = MintVoucher.model_validate(pub["voucher"])
    assert voucher.content_hash == content_hash(pub["metadata"]) and voucher.to == OWNER
    domain = Domain(chain_id=31337, verifying_contract=CARD)
    assert recover(voucher, domain, bytes.fromhex(pub["signature"][2:])) == Account.from_key(KEY).address
    # same draft, same metadata and voucher (reproducible)
    again = json.loads(runner.invoke(app, publish_args(out, "--dry-run")).stdout)
    assert again["metadata"] == pub["metadata"] and again["voucher"] == pub["voucher"]


def test_publish_needs_both_approvals(tmp_path, template_doc):
    out, _ = make_draft(tmp_path, template_doc, "--placeholder")
    result = runner.invoke(app, publish_args(out, "--dry-run"))
    assert result.exit_code == 1 and "needs approval from: moderator, owner" in result.output
    approve(out, "moderator")
    result = runner.invoke(app, publish_args(out, "--dry-run"))
    assert result.exit_code == 1 and "needs approval from: owner" in result.output
    again = runner.invoke(app, ["review", str(out), "--as", "moderator", "--by", "m", "--approve"])
    assert again.exit_code == 1 and "already reviewed" in again.output


def test_rejection_closes_the_draft(tmp_path, template_doc):
    out, _ = make_draft(tmp_path, template_doc, "--placeholder")
    result = runner.invoke(app, ["review", str(out), "--as", "owner", "--by", "p", "--reject", "--note", "wrong face"])
    assert result.exit_code == 0 and json.loads(result.stdout)["status"] == "rejected"
    assert runner.invoke(app, ["review", str(out), "--as", "moderator", "--by", "m", "--approve"]).exit_code == 1
    result = runner.invoke(app, publish_args(out, "--dry-run"))
    assert result.exit_code == 1 and "rejected" in result.output
    assert json.loads((out / "draft.json").read_text())["reviews"][0]["note"] == "wrong face"


def test_publish_refuses_a_changed_image(tmp_path, template_doc):
    out, _ = make_draft(tmp_path, template_doc, "--placeholder")
    approve(out)
    (out / "art.png").write_bytes(b"something else")
    result = runner.invoke(app, publish_args(out, "--dry-run"))
    assert result.exit_code == 1 and "differs" in result.output


def test_draft_refuses_draft_template(tmp_path, template_doc):
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump({**template_doc, "status": "draft"}))
    result = runner.invoke(app, draft_args(path, tmp_path / "d", "--placeholder"))
    assert result.exit_code == 1
    assert "only approved templates render" in result.output


def test_draft_needs_one_image_source_and_a_fresh_directory(tmp_path, template_doc):
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(template_doc))
    assert runner.invoke(app, draft_args(path, tmp_path / "d")).exit_code == 2
    assert runner.invoke(app, draft_args(path, tmp_path / "d", "--placeholder")).exit_code == 0
    result = runner.invoke(app, draft_args(path, tmp_path / "d", "--placeholder"))
    assert result.exit_code == 1 and "already holds a draft" in result.output


def test_live_publish_refuses_placeholder_art_and_needs_ipfs_and_signer(tmp_path, template_doc, monkeypatch):
    out, _ = make_draft(tmp_path, template_doc, "--placeholder")
    approve(out)
    monkeypatch.setenv("ARGUS_MINT_SIGNER_KEY", KEY)
    result = runner.invoke(app, publish_args(out, "--ipfs-api", "http://ipfs:5001"))
    assert result.exit_code == 2 and "placeholder art" in result.output
    image = tmp_path / "art.png"
    image.write_bytes(b"supplied art")
    out2, _ = make_draft(tmp_path, template_doc, "--image", str(image), name="d2")
    approve(out2)
    monkeypatch.delenv("ARGUS_MINT_SIGNER_KEY")
    result = runner.invoke(app, publish_args(out2))
    assert result.exit_code == 2 and "--ipfs-api" in result.output


def test_templates_lists_registry(tmp_path, template_doc):
    (tmp_path / "t.yaml").write_text(yaml.safe_dump(template_doc))
    result = runner.invoke(app, ["templates", str(tmp_path)])
    assert result.exit_code == 0
    assert result.stdout.startswith("test-portrait\tv1\tapproved\t0x")


def test_live_mint_through_darkroom_and_kubo(tmp_path, template_doc, monkeypatch):
    import httpx

    from argus_mint import cli
    from argus_mint.darkroom import DarkroomClient
    from argus_mint.pin import KuboPinner

    from .test_darkroom import FakeDarkroom

    doc = {**template_doc, "sampling": {"sampler": "euler", "scheduler": "normal", "steps": 20, "cfg": 5.0}}
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(doc))
    fake = FakeDarkroom("deleted", "passed")
    pinned = []

    def kubo(request):
        pinned.append(request)
        return httpx.Response(200, json={"Hash": f"bafkrei{'a' * 52}"})

    monkeypatch.setattr(
        cli,
        "DarkroomClient",
        lambda url, **kw: DarkroomClient(url, client=httpx.Client(transport=httpx.MockTransport(fake)), **kw),
    )
    monkeypatch.setattr(
        cli, "KuboPinner", lambda url: KuboPinner(url, client=httpx.Client(transport=httpx.MockTransport(kubo)))
    )
    result = runner.invoke(app, draft_args(path, tmp_path / "d", "--darkroom", "http://darkroom:8106"))
    assert result.exit_code == 0, result.output
    d = json.loads(result.stdout)
    assert [a["outcome"] for a in d["attempts"]] == ["safety_rejected", "ok"]
    assert [a["ref"] for a in d["attempts"]] == ["rec1", "rec2"]
    assert not pinned  # nothing leaves the machine before review
    approve(tmp_path / "d")
    monkeypatch.setenv("ARGUS_MINT_SIGNER_KEY", KEY)
    result = runner.invoke(app, publish_args(tmp_path / "d", "--ipfs-api", "http://ipfs:5001"))
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["dry_run"] is False and out["render"]["renderer"] == "darkroom"
    assert out["metadata"]["provenance"]["attempts"] == 2
    assert out["metadata"]["provenance"]["seed"] == fake.bodies[1]["seed"]
    assert len(pinned) == 2  # image + metadata
    assert all(b["enforcing_policy"] == "default" and b["expand"] is False for b in fake.bodies)


def test_sample_renders_drafts_to_local_files(tmp_path, template_doc, monkeypatch):
    import httpx

    from argus_mint import cli
    from argus_mint.darkroom import DarkroomClient

    from .test_darkroom import FakeDarkroom

    doc = {**template_doc, "status": "draft", "approved_by": None, "approved_at": None}
    doc["sampling"] = {"sampler": "euler", "scheduler": "normal", "steps": 20, "cfg": 5.0}
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(doc))
    fake = FakeDarkroom("passed", "passed", "deleted", "passed")
    monkeypatch.setattr(
        cli,
        "DarkroomClient",
        lambda url, **kw: DarkroomClient(url, client=httpx.Client(transport=httpx.MockTransport(fake)), **kw),
    )
    args = ["sample", str(path), "--out", str(tmp_path / "s"), "--count", "3", "--var", "role=ranger"]
    result = runner.invoke(app, [*args, "--darkroom", "http://darkroom:8106"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["status"] == "draft" and len(out["samples"]) == 3
    assert all(s["variables"]["role"] == "ranger" for s in out["samples"])
    assert len({json.dumps(s["variables"], sort_keys=True) for s in out["samples"]}) == 3
    assert [len(s["attempts"]) for s in out["samples"]] == [1, 1, 2]  # the safety check still applies
    assert len(list((tmp_path / "s").glob("*.png"))) == 3


def test_preflight_command(tmp_path, template_doc, monkeypatch):
    import httpx

    from argus_mint import cli
    from argus_mint.darkroom import DarkroomClient

    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump({**template_doc, "status": "draft", "approved_by": None, "approved_at": None}))
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        refused = "infantry" in body["user_prompt"] and "crimson" in body["user_prompt"]
        return httpx.Response(
            200, json={"outcome": "refused" if refused else "ok", "refusal": "default:X" if refused else None}
        )

    monkeypatch.setattr(
        cli,
        "DarkroomClient",
        lambda url, **kw: DarkroomClient(url, client=httpx.Client(transport=httpx.MockTransport(handler)), **kw),
    )
    result = runner.invoke(app, ["preflight", str(path), "--darkroom", "http://darkroom:8106"])
    assert result.exit_code == 1  # one combination is refused
    assert len(seen) == 8  # 2 x 2 x 2 combinations, drafts included
    assert result.stdout.count("REFUSED") == 2
    assert all(b["sampling"] is None and b["expand"] is False for b in seen)
