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


def _args(path, *extra):
    return [
        "mint",
        "--template", str(path),
        "--var", "role=ranger", "--var", "colour=none", "--var", "pose=standing",
        "--seed", "81234",
        "--asset", "unit-17",
        "--to", "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
        "--name", "Hana of the Strait",
        "--card", CARD,
        "--chain-id", "31337",
        "--consumer", "atlas-realms",
        "--deadline", "1900000000",
        *extra,
    ]  # fmt: skip


def test_mint_dry_run_prints_voucher_and_metadata(tmp_path, template_doc, monkeypatch):
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(template_doc))
    monkeypatch.setenv("ARGUS_MINT_SIGNER_KEY", KEY)
    result = runner.invoke(app, _args(path, "--dry-run"))
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["dry_run"] is True and out["signer_is_ephemeral"] is False
    assert out["metadata"]["provenance"]["template"] == "test-portrait"
    assert out["metadata"]["provenance"]["seed"] == 81234
    assert out["render"]["prompt"] == "portrait of a ranger warrior, none heraldry, standing"
    voucher = MintVoucher.model_validate(out["voucher"])
    assert voucher.content_hash == content_hash(out["metadata"])
    domain = Domain(chain_id=31337, verifying_contract=CARD)
    assert recover(voucher, domain, bytes.fromhex(out["signature"][2:])) == Account.from_key(KEY).address
    # same inputs, same output (reproducible)
    assert runner.invoke(app, _args(path, "--dry-run")).stdout == result.stdout


def test_mint_dry_run_with_ephemeral_key(tmp_path, template_doc, monkeypatch):
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(template_doc))
    monkeypatch.delenv("ARGUS_MINT_SIGNER_KEY", raising=False)
    out = json.loads(runner.invoke(app, _args(path, "--dry-run")).stdout)
    assert out["signer_is_ephemeral"] is True


def test_mint_refuses_draft_template(tmp_path, template_doc):
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump({**template_doc, "status": "draft"}))
    result = runner.invoke(app, _args(path, "--dry-run"))
    assert result.exit_code == 1
    assert "only approved templates render" in result.output


def test_live_mint_requires_darkroom_ipfs_and_signer(tmp_path, template_doc):
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(template_doc))
    result = runner.invoke(app, _args(path))
    assert result.exit_code == 2


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
    monkeypatch.setenv("ARGUS_MINT_SIGNER_KEY", KEY)
    result = runner.invoke(app, _args(path, "--darkroom", "http://darkroom:8106", "--ipfs-api", "http://ipfs:5001"))
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["dry_run"] is False and out["render"]["renderer"] == "darkroom"
    assert [a["outcome"] for a in out["render"]["attempts"]] == ["safety_rejected", "ok"]
    assert [a["ref"] for a in out["render"]["attempts"]] == ["rec1", "rec2"]
    assert out["metadata"]["provenance"]["attempts"] == 2
    assert out["metadata"]["provenance"]["seed"] == fake.bodies[1]["seed"]
    assert len(pinned) == 2  # image + metadata
    assert all(b["enforcing_policy"] == "default" and b["expand"] is False for b in fake.bodies)


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
