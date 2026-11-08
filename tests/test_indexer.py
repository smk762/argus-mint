"""Indexer unit tests against a fake RPC (no network)."""

from __future__ import annotations

import pytest

from argus_mint.indexer import TRANSFER_TOPIC, Cursor, ReorgTooDeep, Transfer, decode_transfer, follow

CARD = "0x" + "ca" * 20
A, B = "0x" + "aa" * 20, "0x" + "bb" * 20


def _topic(addr: str) -> bytes:
    return bytes(12) + bytes.fromhex(addr[2:])


def _log(block: int, index: int, frm: str, to: str, token: int) -> dict:
    return {
        "blockNumber": block,
        "blockHash": block.to_bytes(32, "big"),
        "transactionHash": (1000 + index).to_bytes(32, "big"),
        "logIndex": index,
        "topics": [TRANSFER_TOPIC, _topic(frm), _topic(to), token.to_bytes(32, "big")],
    }


class FakeRpc:
    def __init__(self, head: int, logs: list[dict]):
        self.head = head
        self.logs = logs
        self.hashes: dict[int, bytes] = {}
        self.calls: list[tuple[int, int]] = []

    def block_number(self) -> int:
        return self.head

    def block_hash(self, number: int) -> bytes:
        return self.hashes.get(number, number.to_bytes(32, "big"))

    def get_logs(self, *, address, topic0, from_block, to_block):
        assert address == CARD and topic0 == TRANSFER_TOPIC
        self.calls.append((from_block, to_block))
        return [lg for lg in self.logs if from_block <= lg["blockNumber"] <= to_block][::-1]  # unordered on purpose


def test_decode_transfer():
    t = decode_transfer(_log(5, 2, "0x" + "00" * 20, A, 7))
    assert (t.block_number, t.log_index, t.token_id) == (5, 2, 7)
    assert t.is_mint and not t.is_burn
    assert t.to_address.lower() == A


def test_decode_rejects_erc20_shape():
    lg = _log(1, 0, A, B, 1)
    lg["topics"] = lg["topics"][:3]
    with pytest.raises(ValueError):
        decode_transfer(lg)


def test_follow_delivers_in_chain_order_and_respects_confirmations():
    rpc = FakeRpc(head=10, logs=[_log(3, 1, A, B, 1), _log(3, 0, "0x" + "00" * 20, A, 1), _log(9, 0, B, A, 1)])
    seen: list[Transfer] = []
    cur = follow(rpc, CARD, Cursor(0), confirmations=2, on_transfer=seen.append)
    assert cur.next_block == 9  # blocks 0..8 are confirmed; block 9 isn't yet
    assert [(t.block_number, t.log_index) for t in seen] == [(3, 0), (3, 1)]
    # nothing new is confirmed: cursor unchanged, no callbacks
    assert follow(rpc, CARD, cur, confirmations=2, on_transfer=seen.append) == cur
    rpc.head = 11
    cur = follow(rpc, CARD, cur, confirmations=2, on_transfer=seen.append)
    assert [(t.block_number, t.log_index) for t in seen][-1] == (9, 0)
    assert cur.next_block == 10


def test_follow_pages_by_max_blocks():
    rpc = FakeRpc(head=100, logs=[])
    cur = Cursor(0)
    for _ in range(4):
        cur = follow(rpc, CARD, cur, confirmations=0, on_transfer=lambda t: None, max_blocks=30)
    assert rpc.calls == [(0, 29), (30, 59), (60, 89), (90, 100)]


def test_follow_detects_a_reorg_deeper_than_confirmations():
    rpc = FakeRpc(head=20, logs=[])
    cur = follow(rpc, CARD, Cursor(0), confirmations=5, on_transfer=lambda t: None)
    rpc.hashes[cur.next_block - 1] = b"\xff" * 32  # that block is no longer canonical
    with pytest.raises(ReorgTooDeep):
        follow(rpc, CARD, cur, confirmations=5, on_transfer=lambda t: None)


def test_cursor_json_round_trip():
    c = Cursor(42, b"\x01" * 32)
    assert Cursor.from_json(c.to_json()) == c
    assert Cursor.from_json(Cursor(0).to_json()) == Cursor(0)


def test_bad_arguments():
    with pytest.raises(ValueError):
        follow(FakeRpc(1, []), CARD, Cursor(0), confirmations=-1, on_transfer=lambda t: None)
