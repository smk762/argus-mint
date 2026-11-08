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


def test_mint_without_dry_run_is_not_available_yet(tmp_path, template_doc):
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(template_doc))
    result = runner.invoke(app, _args(path))
    assert result.exit_code == 2


def test_templates_lists_registry(tmp_path, template_doc):
    (tmp_path / "t.yaml").write_text(yaml.safe_dump(template_doc))
    result = runner.invoke(app, ["templates", str(tmp_path)])
    assert result.exit_code == 0
    assert result.stdout.startswith("test-portrait\tv1\tapproved\t0x")
