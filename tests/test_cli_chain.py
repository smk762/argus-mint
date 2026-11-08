"""`submit`, `freeze` and `anchor` against a live anvil node, signing from a Foundry-style keystore."""

from __future__ import annotations

import json

import pytest
import yaml
from eth_account import Account
from typer.testing import CliRunner

from argus_mint import merkle
from argus_mint.cli import app
from argus_mint.keys import KEY_ENV, PASSWORD_FILE_ENV

from .conftest import ANVIL_KEY0

pytestmark = pytest.mark.anvil
runner = CliRunner()
CID = "bafkreif3iky42eg7i5ipwrs47kxqvk5l4gfq5ucyxja4pmsddchc2mu7dm"


@pytest.fixture
def ops(w3, tmp_path):
    """The deployer (anvil account 0) as an encrypted keystore plus a password file."""
    ks = tmp_path / "deployer"
    ks.write_text(json.dumps(Account.encrypt(ANVIL_KEY0, "pw", iterations=2)))
    pw = tmp_path / "pw"
    pw.write_text("pw\n")
    return {"account": str(ks), "env": {PASSWORD_FILE_ENV: str(pw)}, "address": Account.from_key(ANVIL_KEY0).address}


def _chain_args(anvil_url, ops):
    return ["--network", "anvil", "--rpc-url", anvil_url, "--account", ops["account"]]


def _voucher(tmp_path, template_doc, card, to, asset="unit-17"):
    """A `mint` output: dry-run art and pins, signed by anvil account 0, marked live."""
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(template_doc))
    args = [
        "mint", "--template", str(path), "--var", "role=ranger", "--var", "colour=none", "--var", "pose=standing",
        "--asset", asset, "--to", to, "--name", "Hana", "--card", card, "--network", "anvil", "--dry-run",
    ]  # fmt: skip
    result = runner.invoke(app, args, env={KEY_ENV: ANVIL_KEY0})
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    out["dry_run"] = False  # stand-in for a live render; the chain doesn't care about the art
    voucher = tmp_path / f"{asset}.json"
    voucher.write_text(json.dumps(out))
    return voucher


def test_submit_and_freeze(w3, deploy, anvil_url, ops, tmp_path, template_doc):
    admin, player = ops["address"], w3.eth.accounts[3]
    card = deploy("Card", "Cards", "CARD", "", admin, admin, admin, admin, 0)
    voucher = _voucher(tmp_path, template_doc, card.address, player)

    result = runner.invoke(app, ["submit", str(voucher), *_chain_args(anvil_url, ops)], env=ops["env"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["token_id"] == 1 and out["from"] == admin and out["to"] == player
    assert card.functions.ownerOf(1).call() == player
    assert out["metadata_uri"].startswith("ipfs://")

    again = runner.invoke(app, ["submit", str(voucher), *_chain_args(anvil_url, ops)], env=ops["env"])
    assert again.exit_code == 1 and "already minted as token 1" in again.output

    freeze = ["freeze", "--card", card.address, "--token-id", "1", "--uri", CID, *_chain_args(anvil_url, ops)]
    result = runner.invoke(app, freeze, env=ops["env"])
    assert result.exit_code == 0, result.output
    assert card.functions.tokenURI(1).call() == f"ipfs://{CID}"
    again = runner.invoke(app, freeze, env=ops["env"])
    assert again.exit_code == 1 and "already frozen" in again.output


def test_submit_refuses_dry_runs_wrong_chain_and_bad_signers(w3, deploy, anvil_url, ops, tmp_path, template_doc):
    admin = ops["address"]
    outsider = Account.create()
    card = deploy("Card", "Cards", "CARD", "", admin, outsider.address, admin, admin, 0)  # admin can't sign
    voucher = _voucher(tmp_path, template_doc, card.address, w3.eth.accounts[3])

    result = runner.invoke(app, ["submit", str(voucher), *_chain_args(anvil_url, ops)], env=ops["env"])
    assert result.exit_code == 1 and "does not hold SIGNER_ROLE" in result.output

    dry = json.loads(voucher.read_text()) | {"dry_run": True}
    voucher.write_text(json.dumps(dry))
    result = runner.invoke(app, ["submit", str(voucher), *_chain_args(anvil_url, ops)], env=ops["env"])
    assert result.exit_code == 1 and "dry run" in result.output

    other_chain = json.loads(voucher.read_text()) | {"dry_run": False}
    other_chain["typed_data"]["domain"]["chainId"] = 84532
    voucher.write_text(json.dumps(other_chain))
    result = runner.invoke(app, ["submit", str(voucher), *_chain_args(anvil_url, ops)], env=ops["env"])
    assert result.exit_code == 1 and "chain 84532" in result.output


def test_revert_is_explained(w3, deploy, anvil_url, ops):
    admin = ops["address"]
    card = deploy("Card", "Cards", "CARD", "", admin, admin, admin, admin, 0)
    freeze = ["freeze", "--card", card.address, "--token-id", "99", "--uri", CID, *_chain_args(anvil_url, ops)]
    result = runner.invoke(app, freeze, env=ops["env"])
    assert result.exit_code == 1
    assert "ERC721NonexistentToken(99)" in result.output


def test_anchor(w3, deploy, anvil_url, ops, tmp_path):
    from argus_mint.ledger import LedgerClient

    ledger = deploy("Ledger", ops["address"], ops["address"])
    values = [bytes([i]) * 32 for i in range(1, 6)]
    leaves = tmp_path / "leaves.txt"
    leaves.write_text("# battle log, epoch 7\n" + "\n".join("0x" + v.hex() for v in values) + "\n\n")
    tree_file = tmp_path / "tree.json"
    args = ["anchor", str(leaves), "--ledger", ledger.address, "--epoch", "7", "--tree", str(tree_file)]

    result = runner.invoke(app, [*args, *_chain_args(anvil_url, ops)], env=ops["env"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    tree = merkle.load(json.loads(tree_file.read_text()))
    assert out["root"] == "0x" + tree.root.hex() and out["count"] == 5
    client = LedgerClient(w3, ledger.address)
    assert client.root(7).root == tree.root
    assert client.verify(7, values[2], tree.proof(2))

    again = runner.invoke(app, [*args, *_chain_args(anvil_url, ops)], env=ops["env"])
    assert again.exit_code == 1 and "already anchored" in again.output


def test_chain_commands_need_an_account(deploy, anvil_url, ops):
    card = deploy("Card", "Cards", "CARD", "", ops["address"], ops["address"], ops["address"], ops["address"], 0)
    args = ["freeze", "--card", card.address, "--token-id", "1", "--uri", CID, "--network", "anvil"]
    result = runner.invoke(app, [*args, "--rpc-url", anvil_url], env={KEY_ENV: "", "ARGUS_MINT_ACCOUNT": ""})
    assert result.exit_code == 2
    assert "needs a sending account" in result.output
