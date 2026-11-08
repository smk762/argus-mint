"""Client for the Ledger contract: commit, reveal, anchor, and the matching reads.

Needs the ``chain`` extra. Commitments are sha256 (see :func:`commitment_for`), Merkle
roots come from :mod:`argus_mint.merkle`. Per hard rule 6 this runs in the consumer's API
or workers, never in its deterministic game code.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from argus_mint._codec import to_bytes32
from argus_mint.chain import contract, send

__all__ = ["Anchor", "Commitment", "LedgerClient", "commitment_for"]


def commitment_for(secret: bytes) -> bytes:
    """sha256(secret): what ``commit`` publishes and ``reveal`` checks."""
    return hashlib.sha256(secret).digest()


@dataclass(frozen=True)
class Commitment:
    hash: bytes
    committed_at: int  # block timestamp, 0 when absent


@dataclass(frozen=True)
class Anchor:
    root: bytes
    count: int
    anchored_at: int  # block timestamp, 0 when absent


class LedgerClient:
    def __init__(self, w3: Any, address: str, account: Any = None):
        self.w3 = w3
        self.contract = contract(w3, "Ledger", address)
        self.account = account

    # writes (COMMITTER_ROLE for commit/anchor; reveal is open to anyone)
    def commit(self, epoch: int, commitment: bytes) -> Any:
        return send(self.w3, self.contract.functions.commit(epoch, to_bytes32(commitment)), self.account)

    def commit_secret(self, epoch: int, secret: bytes) -> Any:
        return self.commit(epoch, commitment_for(secret))

    def reveal(self, epoch: int, secret: bytes) -> Any:
        return send(self.w3, self.contract.functions.reveal(epoch, secret), self.account)

    def anchor(self, epoch: int, root: bytes, count: int) -> Any:
        return send(self.w3, self.contract.functions.anchor(epoch, to_bytes32(root), count), self.account)

    # reads
    def commitment(self, epoch: int) -> Commitment:
        h, at = self.contract.functions.commitment(epoch).call()
        return Commitment(bytes(h), at)

    def is_revealed(self, epoch: int) -> bool:
        return self.contract.functions.isRevealed(epoch).call()

    def root(self, epoch: int) -> Anchor:
        r, count, at = self.contract.functions.root(epoch).call()
        return Anchor(bytes(r), count, at)

    def verify(self, epoch: int, value: bytes, proof: Sequence[bytes]) -> bool:
        return self.contract.functions.verify(epoch, to_bytes32(value), [to_bytes32(p) for p in proof]).call()

    def revealed_secret(self, epoch: int, from_block: int = 0) -> bytes | None:
        """The secret from the epoch's ``Revealed`` event, or None if not revealed yet."""
        logs = self.contract.events.Revealed.get_logs(from_block=from_block, argument_filters={"epoch": epoch})
        return bytes(logs[0]["args"]["secret"]) if logs else None
