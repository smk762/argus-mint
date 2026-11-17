from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from argus_mint import networks
from argus_mint.cli import app
from argus_mint.indexer import Web3Rpc


def test_base_networks():
    assert networks.BASE.chain_id == 8453
    assert networks.BASE_SEPOLIA.chain_id == 84532
    assert networks.get("base-sepolia") is networks.BASE_SEPOLIA
    assert networks.get("Base_Sepolia") is networks.BASE_SEPOLIA
    assert networks.BASE.address_url("0xabc") == "https://basescan.org/address/0xabc"
    assert networks.ANVIL.tx_url("0x1") is None
    with pytest.raises(ValueError, match="unknown network"):
        networks.get("polygon")


class _Eth:
    block_number = 100

    def get_block(self, tag):
        return {"number": {"safe": 90, "finalized": 70}[tag], "hash": b"\x00" * 32}


class _W3:
    eth = _Eth()


@pytest.mark.parametrize(("head", "expected"), [("latest", 100), ("safe", 90), ("finalized", 70)])
def test_web3rpc_head(head, expected):
    assert Web3Rpc(_W3(), head=head).block_number() == expected


def test_web3rpc_rejects_unknown_head():
    with pytest.raises(ValueError):
        Web3Rpc(_W3(), head="pending")


def test_cli_network_sets_chain_id(tmp_path, template_doc):
    from .test_cli import approve, make_draft

    out, _ = make_draft(tmp_path, template_doc, "--placeholder")
    approve(out)
    base = ["publish", str(out), "--card", "0x5FbDB2315678afecb367f032d93F642f64180aa3", "--dry-run"]
    runner = CliRunner()
    default = json.loads(runner.invoke(app, base).stdout)
    assert default["typed_data"]["domain"]["chainId"] == 84532  # Base Sepolia by default
    mainnet = json.loads(runner.invoke(app, [*base, "--network", "base"]).stdout)
    assert mainnet["typed_data"]["domain"]["chainId"] == 8453
    assert runner.invoke(app, [*base, "--network", "polygon"]).exit_code != 0


def test_rpc_errors_never_carry_the_provider_key():
    pytest.importorskip("web3")
    from argus_mint.chain import NoRpcAvailable, connect, redact_url

    urls = ["http://127.0.0.1:1/v3/SECRETKEY?apikey=SECRET2", "http://user:SECRET3@127.0.0.1:1/"]
    with pytest.raises(NoRpcAvailable) as e:
        connect("base", urls, timeout=0.5)
    assert "SECRET" not in str(e.value) and "http://127.0.0.1:1" in str(e.value)
    assert redact_url("https://eth-mainnet.g.alchemy.com/v2/KEY") == "https://eth-mainnet.g.alchemy.com"
    assert redact_url("not a url") == "<rpc url>"
