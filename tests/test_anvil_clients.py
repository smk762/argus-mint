"""The chain clients against a live anvil node: Ledger client and a reorg-safe indexer replay."""

from __future__ import annotations

import hashlib

import pytest
from eth_account import Account

from argus_mint import merkle
from argus_mint.vouchers import Domain, MintVoucher, sign

from .conftest import ANVIL_KEY0

pytestmark = pytest.mark.anvil

ZERO = "0x" + "00" * 20


def _mine(w3, n: int) -> None:
    for _ in range(n):
        w3.provider.make_request("evm_mine", [])


def test_ledger_client(w3, deploy):
    from argus_mint.ledger import LedgerClient, commitment_for

    committer = Account.from_key(ANVIL_KEY0)  # signs locally (the production path)
    ledger_contract = deploy("Ledger", w3.eth.accounts[0], committer.address)
    client = LedgerClient(w3, ledger_contract.address, committer)

    secret = bytes(range(32))
    client.commit_secret(20_400, secret)
    c = client.commitment(20_400)
    assert c.hash == commitment_for(secret) and c.committed_at > 0
    assert client.revealed_secret(20_400) is None

    LedgerClient(w3, ledger_contract.address, w3.eth.accounts[5]).reveal(20_400, secret)  # anyone may reveal
    assert client.is_revealed(20_400)
    assert client.revealed_secret(20_400) == secret

    values = [hashlib.sha256(f"log-{i}".encode()).digest() for i in range(9)]
    tree = merkle.build(values)
    client.anchor(20_400, tree.root, len(values))
    a = client.root(20_400)
    assert (a.root, a.count) == (tree.root, 9) and a.anchored_at > 0
    assert client.verify(20_400, values[4], tree.proof(4))
    assert not client.verify(20_400, values[4], tree.proof(5))

    from web3.exceptions import ContractCustomError

    with pytest.raises(ContractCustomError):  # second anchor for the epoch reverts (AlreadyAnchored)
        client.anchor(20_400, tree.root, len(values))


def test_indexer_survives_a_reorg(w3, deploy):
    from argus_mint.indexer import Cursor, ReorgTooDeep, Transfer, Web3Rpc, follow

    admin, alice, bob, carol = w3.eth.accounts[:4]
    signer = Account.from_key(ANVIL_KEY0)
    card = deploy("Card", "Cards", "CARD", "", admin, signer.address, admin, admin, 0)
    domain = Domain(chain_id=w3.eth.chain_id, verifying_contract=card.address)
    deadline = w3.eth.get_block("latest").timestamp + 3600
    for i in (1, 2):
        v = MintVoucher(to=alice, asset_id=bytes([i]) * 32, content_hash=b"\x00" * 32, price=0, deadline=deadline)
        card.functions.mint((v.to, v.asset_id, v.content_hash, v.price, v.deadline), sign(v, domain, signer)).transact(
            {"from": admin}
        )

    rpc = Web3Rpc(w3)

    def indexer(confirmations: int):
        owners: dict[int, str] = {}
        state = {"cursor": Cursor(0)}

        def apply(t: Transfer) -> None:
            if t.to_address == ZERO:
                owners.pop(t.token_id, None)
            else:
                owners[t.token_id] = t.to_address

        def step() -> None:
            state["cursor"] = follow(rpc, card.address, state["cursor"], confirmations, apply)

        return owners, step, state

    safe_owners, safe_step, safe_state = indexer(confirmations=3)
    naive_owners, naive_step, _ = indexer(confirmations=0)

    _mine(w3, 3)
    safe_step()
    naive_step()
    assert safe_owners == naive_owners == {1: alice, 2: alice}

    # A transfer lands, both indexers poll, then a reorg drops it.
    snap = w3.provider.make_request("evm_snapshot", [])["result"]
    card.functions.transferFrom(alice, bob, 1).transact({"from": alice})
    safe_step()
    naive_step()
    assert naive_owners[1] == bob  # the naive indexer believed an unconfirmed block
    assert safe_owners[1] == alice  # the safe one hadn't read it yet
    w3.provider.make_request("evm_revert", [snap])

    # The canonical chain has a different transfer instead.
    card.functions.transferFrom(alice, carol, 2).transact({"from": alice})
    _mine(w3, 3)
    safe_step()
    truth = {i: card.functions.ownerOf(i).call() for i in (1, 2)}
    assert truth == {1: alice, 2: carol}
    assert safe_owners == truth
    # The 0-confirmation indexer had read the dropped block; its cursor's block hash no longer
    # matches the chain, so it stops instead of carrying the stale owner forward.
    with pytest.raises(ReorgTooDeep):
        naive_step()
    assert naive_owners[1] == bob  # what it would have kept without the check

    # A reorg deeper than the confirmations is detected, not silently skipped.
    deep = w3.provider.make_request("evm_snapshot", [])["result"]
    _mine(w3, 5)
    safe_step()
    w3.provider.make_request("evm_revert", [deep])
    card.functions.transferFrom(carol, bob, 2).transact({"from": carol})
    _mine(w3, 5)
    with pytest.raises(ReorgTooDeep):
        safe_step()


def test_connect_fails_over_and_checks_chain_id(anvil_url):
    from argus_mint import networks
    from argus_mint.chain import NoRpcAvailable, connect

    w3 = connect(networks.ANVIL, ["http://127.0.0.1:1", anvil_url], timeout=2)
    assert w3.eth.chain_id == 31337
    with pytest.raises(NoRpcAvailable, match="expected 8453"):
        connect("base", [anvil_url], timeout=2)  # answers, but it's the wrong chain
