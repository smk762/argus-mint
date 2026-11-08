"""Merkle trees and proofs, byte-compatible with OpenZeppelin's ``StandardMerkleTree``
(``@openzeppelin/merkle-tree``) using leaf encoding ``["bytes32"]``, and therefore with
Solidity's ``MerkleProof.verify``. Pure; no I/O.

- leaf  = keccak256(keccak256(abi.encode(bytes32 value))) = keccak256(keccak256(value))
- pairs are hashed in sorted order: keccak256(min(a, b) ‖ max(a, b))
- leaves are sorted by hash before the tree is laid out (OZ ``sortLeaves: true``)
- the tree is a flat array of 2n-1 nodes, root at 0, leaves at the end in reverse order

The consumer supplies the 32-byte values (atlas-realms: sha256 of each battle log's
canonical JSON); argus-mint never decides what a leaf means.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from argus_mint._codec import hex0x, keccak256, to_bytes32

__all__ = ["Tree", "build", "hash_pair", "leaf", "load", "process_proof", "verify"]


def leaf(value: bytes) -> bytes:
    """Leaf hash of a 32-byte value (``abi.encode(bytes32)`` is the value itself)."""
    return keccak256(keccak256(to_bytes32(value)))


def hash_pair(a: bytes, b: bytes) -> bytes:
    return keccak256(a + b) if a <= b else keccak256(b + a)


def process_proof(leaf_hash: bytes, proof: Sequence[bytes]) -> bytes:
    node = leaf_hash
    for sibling in proof:
        node = hash_pair(node, to_bytes32(sibling))
    return node


def verify(root: bytes, value: bytes, proof: Sequence[bytes]) -> bool:
    """True when ``proof`` shows ``value`` is a leaf of the tree with ``root``."""
    return process_proof(leaf(value), proof) == to_bytes32(root)


@dataclass(frozen=True)
class Tree:
    """A built tree. ``values[i]`` keeps the caller's order; ``proof(i)`` refers to it."""

    nodes: tuple[bytes, ...]
    values: tuple[bytes, ...]
    tree_index: tuple[int, ...]  # tree_index[i] = position of values[i]'s leaf in nodes

    @property
    def root(self) -> bytes:
        return self.nodes[0]

    def __len__(self) -> int:
        return len(self.values)

    def proof(self, index: int) -> list[bytes]:
        """Sibling hashes from leaf ``values[index]`` up to (excluding) the root."""
        if not 0 <= index < len(self.values):
            raise IndexError(f"no value at index {index}")
        i = self.tree_index[index]
        out: list[bytes] = []
        while i > 0:
            sibling = i + 1 if i % 2 else i - 1
            out.append(self.nodes[sibling])
            i = (i - 1) // 2
        return out

    def dump(self) -> dict[str, Any]:
        """The ``standard-v1`` JSON dump format of ``StandardMerkleTree.dump()``."""
        return {
            "format": "standard-v1",
            "leafEncoding": ["bytes32"],
            "tree": [hex0x(n) for n in self.nodes],
            "values": [
                {"value": [hex0x(v)], "treeIndex": t} for v, t in zip(self.values, self.tree_index, strict=True)
            ],
        }


def build(values: Sequence[bytes]) -> Tree:
    """Build a tree over 32-byte ``values`` (raw bytes or 0x hex). At least one value."""
    vals = tuple(to_bytes32(v) for v in values)
    if not vals:
        raise ValueError("a Merkle tree needs at least one value")
    hashed = sorted(((leaf(v), i) for i, v in enumerate(vals)), key=lambda p: p[0])
    n = len(hashed)
    nodes: list[bytes] = [b""] * (2 * n - 1)
    tree_index = [0] * n
    for k, (h, value_index) in enumerate(hashed):
        pos = len(nodes) - 1 - k
        nodes[pos] = h
        tree_index[value_index] = pos
    for i in range(len(nodes) - 1 - n, -1, -1):
        nodes[i] = hash_pair(nodes[2 * i + 1], nodes[2 * i + 2])
    return Tree(nodes=tuple(nodes), values=vals, tree_index=tuple(tree_index))


def load(dump: dict[str, Any]) -> Tree:
    """Load a ``standard-v1`` dump (from :meth:`Tree.dump` or the JS library) and check it.

    Every leaf is re-hashed from its value and every inner node recomputed, so a tampered
    dump is rejected rather than trusted.
    """
    if dump.get("format") != "standard-v1" or dump.get("leafEncoding") != ["bytes32"]:
        raise ValueError("expected a standard-v1 dump with leafEncoding ['bytes32']")
    nodes = tuple(to_bytes32(h) for h in dump["tree"])
    values = tuple(to_bytes32(entry["value"][0]) for entry in dump["values"])
    tree_index = tuple(int(entry["treeIndex"]) for entry in dump["values"])
    n = len(values)
    if n == 0 or len(nodes) != 2 * n - 1:
        raise ValueError("tree size doesn't match the number of values")
    first_leaf = len(nodes) - n
    if sorted(tree_index) != list(range(first_leaf, len(nodes))):
        raise ValueError("treeIndex values don't cover the leaves exactly once")
    for v, t in zip(values, tree_index, strict=True):
        if nodes[t] != leaf(v):
            raise ValueError(f"leaf at treeIndex {t} doesn't match its value")
    for i in range(first_leaf):
        if nodes[i] != hash_pair(nodes[2 * i + 1], nodes[2 * i + 2]):
            raise ValueError(f"inner node {i} doesn't match its children")
    return Tree(nodes=nodes, values=values, tree_index=tree_index)
