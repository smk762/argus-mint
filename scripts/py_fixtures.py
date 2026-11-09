"""Python-built fixtures the forge tests consume: signed mint vouchers and Ledger proofs.

They prove the Python side and the contracts agree (a Python-signed voucher mints; a
Python-built proof verifies in Ledger.verify). tests/test_fixtures.py fails if the
committed copies drift from what this script produces. Run via `make fixtures`.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_utils import to_checksum_address

from argus_mint import merkle
from argus_mint.metadata import Attribute, Provenance, card_metadata, content_hash
from argus_mint.vouchers import Domain, MintVoucher, sign, voucher_digest

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# anvil's default account 0: a public test key that never holds real funds.
ANVIL_KEY0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
CARD_ADDRESS = to_checksum_address("0x" + "ca" * 20)  # forge tests deploy Card here (deployCodeTo)
CHAIN_ID = 31337
CID = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"


def _h(b: bytes) -> str:
    return "0x" + b.hex()


def vouchers() -> dict[str, Any]:
    signer = Account.from_key(ANVIL_KEY0)
    domain = Domain(chain_id=CHAIN_ID, verifying_contract=CARD_ADDRESS)
    out = []
    for i, price in enumerate([0, 10**15]):
        asset_id = hashlib.sha256(f"argus-mint/asset/{i}".encode()).digest()
        doc = card_metadata(
            name=f"Fixture card {i}",
            description="Cross-implementation fixture.",
            image_cid=CID,
            external_url=None,
            attributes=[Attribute(trait_type="Index", value=i, display_type="number")],
            provenance=Provenance(
                template="fixture",
                template_version=1,
                template_hash=b"\x11" * 32,
                seed=1000 + i,
                attempts=1,
                asset_id=asset_id,
                consumer="fixtures",
            ),
        )
        v = MintVoucher(
            to=to_checksum_address("0x" + f"{i + 1:02x}" * 20),
            asset_id=asset_id,
            content_hash=content_hash(doc),
            price=price,
            deadline=2_000_000_000,
        )
        out.append(
            {
                "to": v.to,
                "assetId": _h(v.asset_id),
                "contentHash": _h(v.content_hash),
                "price": str(v.price),
                "deadline": v.deadline,
                "digest": _h(voucher_digest(v, domain)),
                "signature": _h(sign(v, domain, signer)),
            }
        )
    return {"chainId": CHAIN_ID, "card": CARD_ADDRESS, "signer": signer.address, "vouchers": out}


def proofs() -> dict[str, Any]:
    cases = []
    for n in (2, 7, 100):
        values = [hashlib.sha256(f"argus-mint/ledger/{n}/{i}".encode()).digest() for i in range(n)]
        tree = merkle.build(values)
        cases.append(
            {
                "count": n,
                "root": _h(tree.root),
                "values": [_h(v) for v in values],
                "proofs": [[_h(p) for p in tree.proof(i)] for i in range(n)],
            }
        )
    return {"cases": cases}


def render() -> dict[str, str]:
    return {
        name: json.dumps(fn(), indent=1) + "\n" for name, fn in (("vouchers.json", vouchers), ("proofs.json", proofs))
    }


def main() -> int:
    for name, text in render().items():
        (OUT / name).write_text(text)
        print(f"wrote tests/fixtures/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
