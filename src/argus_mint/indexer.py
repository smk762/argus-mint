"""A reorg-safe follower for ERC-721 ``Transfer`` events (Card and Deed).

The indexer keeps no state. Each :func:`follow` call reads confirmed blocks from the
cursor onward, calls back once per transfer in chain order, and returns the next cursor.
The consumer persists the cursor and its owner rows in the same DB transaction as the
callback's writes, and loops. Only blocks with ``confirmations`` on top are read, so an
ordinary reorg can't leave a stale owner; a reorg deeper than that is detected (the
cursor remembers the hash of the last block it read) and raised, never silently skipped.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from eth_utils import to_checksum_address

from argus_mint._codec import keccak256

__all__ = ["TRANSFER_TOPIC", "Cursor", "ReorgTooDeep", "Rpc", "Transfer", "Web3Rpc", "follow"]

TRANSFER_TOPIC = keccak256(b"Transfer(address,address,uint256)")
ZERO_ADDRESS = "0x" + "00" * 20


class Rpc(Protocol):
    def block_number(self) -> int: ...

    def block_hash(self, number: int) -> bytes: ...

    def get_logs(self, *, address: str, topic0: bytes, from_block: int, to_block: int) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Cursor:
    next_block: int  # first block not yet read
    last_hash: bytes | None = None  # hash of block next_block - 1 when it was read

    def to_json(self) -> dict[str, Any]:
        return {"next_block": self.next_block, "last_hash": self.last_hash.hex() if self.last_hash else None}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Cursor:
        h = d.get("last_hash")
        return cls(int(d["next_block"]), bytes.fromhex(h) if h else None)


@dataclass(frozen=True)
class Transfer:
    block_number: int
    block_hash: bytes
    tx_hash: bytes
    log_index: int
    from_address: str
    to_address: str
    token_id: int

    @property
    def is_mint(self) -> bool:
        return self.from_address == ZERO_ADDRESS

    @property
    def is_burn(self) -> bool:
        return self.to_address == ZERO_ADDRESS


class ReorgTooDeep(Exception):
    """A block the cursor already read is no longer canonical (reorg deeper than confirmations)."""


def _topic_address(topic: bytes) -> str:
    return to_checksum_address("0x" + bytes(topic)[-20:].hex())


def decode_transfer(log: dict[str, Any]) -> Transfer:
    topics = [bytes(t) for t in log["topics"]]
    if len(topics) != 4 or topics[0] != TRANSFER_TOPIC:
        raise ValueError("not an ERC-721 Transfer log (expects 3 indexed arguments)")
    return Transfer(
        block_number=int(log["blockNumber"]),
        block_hash=bytes(log["blockHash"]),
        tx_hash=bytes(log["transactionHash"]),
        log_index=int(log["logIndex"]),
        from_address=_topic_address(topics[1]),
        to_address=_topic_address(topics[2]),
        token_id=int.from_bytes(topics[3], "big"),
    )


def follow(
    rpc: Rpc,
    contract: str,
    cursor: Cursor,
    confirmations: int,
    on_transfer: Callable[[Transfer], None],
    *,
    max_blocks: int = 2_000,
) -> Cursor:
    """One pass: deliver confirmed transfers after ``cursor`` (up to ``max_blocks`` blocks)."""
    if confirmations < 0 or max_blocks < 1:
        raise ValueError("confirmations must be >= 0 and max_blocks >= 1")
    if (
        cursor.last_hash is not None
        and cursor.next_block > 0
        and rpc.block_hash(cursor.next_block - 1) != cursor.last_hash
    ):
        raise ReorgTooDeep(f"block {cursor.next_block - 1} changed after it was indexed")
    safe = rpc.block_number() - confirmations
    if safe < cursor.next_block:
        return cursor
    to_block = min(safe, cursor.next_block + max_blocks - 1)
    logs = rpc.get_logs(address=contract, topic0=TRANSFER_TOPIC, from_block=cursor.next_block, to_block=to_block)
    transfers = sorted((decode_transfer(lg) for lg in logs), key=lambda t: (t.block_number, t.log_index))
    for t in transfers:
        on_transfer(t)
    return Cursor(next_block=to_block + 1, last_hash=rpc.block_hash(to_block))


class Web3Rpc:
    """:class:`Rpc` over a web3 ``Web3`` instance (the ``chain`` extra)."""

    def __init__(self, w3: Any):
        self.w3 = w3

    def block_number(self) -> int:
        return int(self.w3.eth.block_number)

    def block_hash(self, number: int) -> bytes:
        return bytes(self.w3.eth.get_block(number)["hash"])

    def get_logs(self, *, address: str, topic0: bytes, from_block: int, to_block: int) -> list[dict[str, Any]]:
        return list(
            self.w3.eth.get_logs(
                {
                    "address": to_checksum_address(address),
                    "topics": ["0x" + topic0.hex()],
                    "fromBlock": from_block,
                    "toBlock": to_block,
                }
            )
        )
