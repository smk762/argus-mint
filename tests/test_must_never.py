"""Checks for rows of CLAUDE.md's "Must never happen" table that no behaviour test covers.

Each test names the rule it holds. If one fails, the rule was broken: fix the change, not the test.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

PURE_CORE = ("_codec", "templates", "metadata", "merkle", "vouchers", "siwe", "projects", "spoilers")
NETWORK_MODULES = (
    "httpx",
    "requests",
    "urllib3",
    "aiohttp",
    "websockets",
    "web3",
    *(f"argus_mint.{m}" for m in ("chain", "ledger", "indexer", "darkroom", "pin", "render", "bios", "gpu", "server")),
)


def test_the_pure_core_imports_no_network_client():
    """Hard rule 8: the pure core does no I/O, so it imports no HTTP, RPC or IPFS client."""
    code = (
        "import sys, json\n"
        + "".join(f"import argus_mint.{m}\n" for m in PURE_CORE)
        + f"print(json.dumps(sorted(m for m in {list(NETWORK_MODULES)!r} if m in sys.modules)))"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}  # this tree's code, whatever is installed
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env).stdout
    assert json.loads(out) == [], "a pure-core module pulled in a network client"


def _abi(name: str) -> list[dict]:
    doc = json.loads((ROOT / "src" / "argus_mint" / "abi" / f"{name}.json").read_text())
    return doc["abi"] if isinstance(doc, dict) else doc


PAYOUT_WORDS = re.compile(r"stake|reward|dividend|yield|claim|distribut|payout|share|interest|vest", re.IGNORECASE)
ERC20_ONLY = {"transfer", "decimals", "allowance", "increaseAllowance", "decreaseAllowance"}


@pytest.mark.parametrize("contract", ["Card", "Deed", "Ledger"])
def test_no_contract_pays_out(contract):
    """Hard rule 5: no fungible token, revenue share or staking. The only ETH in is a card's
    price (Card.mint); the only way out is the admin's withdraw of those proceeds."""
    fns = [x for x in _abi(contract) if x["type"] == "function"]
    names = {f["name"] for f in fns}
    assert not names & ERC20_ONLY, f"{contract} has an ERC-20 surface"
    assert not [n for n in names if PAYOUT_WORDS.search(n)], f"{contract} has a payout-shaped function"
    payable = sorted(f["name"] for f in fns if f.get("stateMutability") == "payable")
    assert payable == (["mint"] if contract == "Card" else [])


def test_no_cli_option_takes_a_key():
    """A signer key never goes on the command line: --account names a keystore, else $ARGUS_MINT_SIGNER_KEY."""
    pytest.importorskip("typer")
    import typer.main

    from argus_mint.cli import app

    found: list[str] = []

    def walk(cmd, path: str) -> None:
        for p in cmd.params:
            for opt in (p.name or "", *getattr(p, "opts", ())):
                if re.search(r"private|secret|mnemonic|seed.?phrase|signer.?key|^-*key$", opt, re.IGNORECASE):
                    found.append(f"{path} {opt}")
        for name, sub in getattr(cmd, "commands", {}).items():
            walk(sub, f"{path} {name}")

    walk(typer.main.get_command(app), "argus-mint")
    assert found == []


# OpenZeppelin v5.4.0. OZ >= 5.5 emits `clz`, which needs Osaka; bump this, the submodule and
# evm_version together, and only after checking Base runs Osaka (CLAUDE.md, Settled decisions).
OZ_V5_4_0 = "c64a1edb67b6e3f4a15cca8909c9482ad33a02b0"


def test_openzeppelin_and_evm_version_move_together():
    foundry = tomllib.loads((ROOT / "contracts" / "foundry.toml").read_text())
    assert foundry["profile"]["default"]["evm_version"] == "cancun"
    try:
        tree = subprocess.run(
            ["git", "-C", str(ROOT), "ls-tree", "HEAD", "contracts/lib/openzeppelin-contracts"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    if not tree:
        pytest.skip("not a git checkout")
    assert tree[2] == OZ_V5_4_0, "OpenZeppelin moved: move evm_version with it (and this pin)"


def test_kubo_rpc_is_published_on_localhost_only_by_default():
    """The Kubo RPC (5001) is admin-level: compose publishes it on 127.0.0.1 unless overridden."""
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    ports = compose["services"]["kubo"]["ports"]
    rpc = [p for p in ports if str(p).endswith(":5001")]
    assert rpc and all(re.fullmatch(r"\$\{[A-Z_]+:-127\.0\.0\.1:5001\}:5001", p) for p in rpc), rpc
