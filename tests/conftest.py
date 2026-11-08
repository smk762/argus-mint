from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def template_doc() -> dict[str, Any]:
    """An approved template as it appears in YAML (before validation)."""
    return {
        "id": "test-portrait",
        "version": 1,
        "status": "approved",
        "approved_by": "reviewer",
        "approved_at": "2026-11-08",
        "model": "juggernaut-xl",
        "loras": ["fantasy-portrait"],
        "width": 1024,
        "height": 1024,
        "prompt": "portrait of a {role} warrior, {colour} heraldry, {pose}",
        "negative": "text, watermark",
        "variables": {
            "role": ["infantry", "ranger"],
            "colour": ["crimson", "none"],
            "pose": ["standing", "on guard"],
        },
        "max_attempts": 4,
    }


@pytest.fixture(scope="session")
def merkle_fixtures() -> dict[str, Any]:
    return json.loads((FIXTURES / "merkle.json").read_text())


# --- anvil (local chain) -------------------------------------------------------------------
# Tests marked `anvil` start a throwaway anvil node and deploy from contracts/out. They skip
# when anvil or the build artifacts are missing, unless ARGUS_MINT_REQUIRE_ANVIL=1 (CI).


ROOT = Path(__file__).parent.parent
CONTRACTS_OUT = ROOT / "contracts" / "out"
ANVIL_KEY0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def _require_or_skip(reason: str) -> None:
    if os.environ.get("ARGUS_MINT_REQUIRE_ANVIL") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


def _anvil_bin() -> str | None:
    return shutil.which("anvil") or next(
        (p for p in [str(Path.home() / ".foundry" / "bin" / "anvil")] if Path(p).exists()), None
    )


@pytest.fixture(scope="session")
def anvil_url():
    exe = _anvil_bin()
    if exe is None:
        _require_or_skip("anvil not installed")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [exe, "--port", str(port), "--silent", "--chain-id", "31337"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        else:
            pytest.fail("anvil did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.fixture(scope="session")
def artifact():
    def load(name: str) -> dict[str, Any]:
        path = CONTRACTS_OUT / f"{name}.sol" / f"{name}.json"
        if not path.exists():
            _require_or_skip(f"{path} missing; run `make forge-build`")
        data = json.loads(path.read_text())
        return {"abi": data["abi"], "bytecode": data["bytecode"]["object"]}

    return load


@pytest.fixture
def w3(anvil_url):
    pytest.importorskip("web3")
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(anvil_url))
    snap = w3.provider.make_request("evm_snapshot", [])["result"]
    yield w3
    w3.provider.make_request("evm_revert", [snap])


@pytest.fixture
def deploy(w3, artifact):
    def _deploy(name: str, *args: Any):
        a = artifact(name)
        contract = w3.eth.contract(abi=a["abi"], bytecode=a["bytecode"])
        tx = contract.constructor(*args).transact({"from": w3.eth.accounts[0]})
        receipt = w3.eth.wait_for_transaction_receipt(tx)
        return w3.eth.contract(address=receipt.contractAddress, abi=a["abi"])

    return _deploy
