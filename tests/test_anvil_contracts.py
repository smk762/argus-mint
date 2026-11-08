"""End to end on a real anvil node: Python-signed vouchers mint, Python-built proofs verify."""

from __future__ import annotations

import hashlib

import pytest
from eth_account import Account

from argus_mint import merkle
from argus_mint._codec import keccak256
from argus_mint.vouchers import Domain, MintVoucher, sign

from .conftest import ANVIL_KEY0

ContractCustomError = pytest.importorskip("web3.exceptions").ContractCustomError

pytestmark = pytest.mark.anvil


def test_python_signed_voucher_mints(w3, deploy):
    admin = w3.eth.accounts[0]
    signer = Account.from_key(ANVIL_KEY0)
    card = deploy("Card", "Cards", "CARD", "https://x.example/", admin, signer.address, admin, admin, 0)
    domain = Domain(chain_id=w3.eth.chain_id, verifying_contract=card.address)
    player = w3.eth.accounts[1]
    deadline = w3.eth.get_block("latest").timestamp + 3600
    v = MintVoucher(to=player, asset_id=b"\x01" * 32, content_hash=b"\x02" * 32, price=10**15, deadline=deadline)
    sig = sign(v, domain, signer)
    args = (v.to, v.asset_id, v.content_hash, v.price, v.deadline)

    relayer = w3.eth.accounts[2]
    tx = card.functions.mint(args, sig).transact({"from": relayer, "value": v.price})
    receipt = w3.eth.wait_for_transaction_receipt(tx)
    assert receipt.status == 1
    assert card.functions.ownerOf(1).call() == player
    assert card.functions.contentHashOf(1).call() == v.content_hash

    # the same voucher can't mint twice
    with pytest.raises(ContractCustomError) as err:
        card.functions.mint(args, sig).transact({"from": relayer, "value": v.price})
    selector = "0x" + keccak256(b"AssetAlreadyMinted(bytes32,uint256)")[:4].hex()
    assert err.value.data.startswith(selector)


def test_python_built_proofs_verify_in_ledger(w3, deploy):
    admin = w3.eth.accounts[0]
    ledger = deploy("Ledger", admin, admin)
    values = [hashlib.sha256(f"battle-{i}".encode()).digest() for i in range(25)]
    tree = merkle.build(values)
    epoch = 20_400
    w3.eth.wait_for_transaction_receipt(
        ledger.functions.anchor(epoch, tree.root, len(values)).transact({"from": admin})
    )
    for i in (0, 7, 24):
        assert ledger.functions.verify(epoch, values[i], tree.proof(i)).call()
    assert not ledger.functions.verify(epoch, values[1], tree.proof(0)).call()


def test_commit_reveal_with_daily_secret(w3, deploy):
    admin = w3.eth.accounts[0]
    ledger = deploy("Ledger", admin, admin)
    secret = bytes(range(32))
    commitment = hashlib.sha256(secret).digest()
    w3.eth.wait_for_transaction_receipt(ledger.functions.commit(1, commitment).transact({"from": admin}))
    w3.eth.wait_for_transaction_receipt(ledger.functions.reveal(1, secret).transact({"from": w3.eth.accounts[3]}))
    assert ledger.functions.isRevealed(1).call()
    stored, _ = ledger.functions.commitment(1).call()
    assert stored.hex() == hashlib.sha256(secret).hexdigest()
