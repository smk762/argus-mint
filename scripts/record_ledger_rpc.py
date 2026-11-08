"""Record real JSON-RPC responses from a Ledger on anvil for the JS verifier's tests.

Starts anvil, deploys Ledger from contracts/out, commits/reveals/anchors, then records the
exact eth_call / eth_getLogs exchanges the verifier makes into tests/fixtures/ledger_rpc.json.
Needs Foundry (anvil) and `make forge-build`. Run via `make fixtures-rpc`.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from eth_abi import encode
from web3 import Web3

from argus_mint._codec import keccak256

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "ledger_rpc.json"
SECRET = bytes(range(32))


def sel(sig: str) -> bytes:
    return keccak256(sig.encode())[:4]


def main() -> int:
    anvil = shutil.which("anvil") or str(Path.home() / ".foundry/bin/anvil")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([anvil, "--port", str(port), "--silent"], stdout=subprocess.DEVNULL)
    try:
        time.sleep(1)
        w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}"))
        art = json.loads((ROOT / "contracts/out/Ledger.sol/Ledger.json").read_text())
        admin = w3.eth.accounts[0]
        tx = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"]).constructor(admin, admin)
        addr = w3.eth.wait_for_transaction_receipt(tx.transact({"from": admin})).contractAddress
        ledger = w3.eth.contract(address=addr, abi=art["abi"])
        proofs = json.loads((ROOT / "tests/fixtures/proofs.json").read_text())["cases"][1]  # 7 leaves

        def wait(fn, sender=admin):
            w3.eth.wait_for_transaction_receipt(fn.transact({"from": sender}))

        wait(ledger.functions.commit(20400, hashlib.sha256(SECRET).digest()))
        wait(ledger.functions.reveal(20400, SECRET), w3.eth.accounts[1])
        wait(ledger.functions.anchor(20400, bytes.fromhex(proofs["root"][2:]), proofs["count"]))
        wait(ledger.functions.commit(20401, hashlib.sha256(b"tomorrow").digest()))

        calls = []

        def record(method: str, params: list) -> None:
            resp = w3.provider.make_request(method, params)
            calls.append({"method": method, "params": params, "result": resp["result"]})

        lower = addr.lower()
        for epoch in (20400, 20401, 20402):
            e = encode(["uint64"], [epoch])
            record("eth_call", [{"to": lower, "data": "0x" + (sel("root(uint64)") + e).hex()}, "latest"])
            record("eth_call", [{"to": lower, "data": "0x" + (sel("commitment(uint64)") + e).hex()}, "latest"])
            topic = "0x" + keccak256(b"Revealed(uint64,bytes)").hex()
            record(
                "eth_getLogs",
                [{"address": lower, "fromBlock": "0x0", "toBlock": "latest", "topics": [topic, "0x" + e.hex()]}],
            )
        for i in (0, 3):
            data = sel("verify(uint64,bytes32,bytes32[])") + encode(
                ["uint64", "bytes32", "bytes32[]"],
                [20400, bytes.fromhex(proofs["values"][i][2:]), [bytes.fromhex(p[2:]) for p in proofs["proofs"][i]]],
            )
            record("eth_call", [{"to": lower, "data": "0x" + data.hex()}, "latest"])
        doc = {
            "ledger": lower,
            "secret": "0x" + SECRET.hex(),
            "tree": "proofs.json cases[1]",
            "calls": calls,
        }
        OUT.write_text(json.dumps(doc, indent=1) + "\n")
        print(f"wrote {OUT.relative_to(ROOT)} ({len(calls)} calls)")
    finally:
        proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
