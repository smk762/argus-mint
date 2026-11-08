"""Chains argus-mint targets. Base (an OP Stack rollup) is the production chain.

RPC: argus-mint talks to plain JSON-RPC endpoints; no explorer API is involved in deploying,
minting, reading or indexing. Each network lists public endpoints in failover order
(``argus_mint.chain.connect`` tries them in turn). Public endpoints are rate-limited and cap
``eth_getLogs`` ranges, so keep ``indexer.follow(max_blocks=...)`` modest (500) on them, and
use a provider (or your own node) for production.

Indexing on Base: the ``latest`` head is the sequencer's unsafe head, which can still be
reorged; ``safe`` is derived from data posted to Ethereum (minutes behind) and ``finalized``
follows Ethereum finality (roughly 15-20 minutes). Follow ``safe`` with
``Web3Rpc(w3, head="safe")`` and ``confirmations=0``, rather than counting blocks.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ANVIL", "BASE", "BASE_SEPOLIA", "NETWORKS", "Network", "get"]


@dataclass(frozen=True)
class Network:
    name: str
    chain_id: int
    public_rpcs: tuple[str, ...]  # failover order; rate-limited, use a provider in production
    explorer: str | None
    block_time: float  # seconds
    blockscout_api: str | None = None  # free contract verification (forge --verifier blockscout)

    @property
    def public_rpc(self) -> str:
        return self.public_rpcs[0]

    def address_url(self, address: str) -> str | None:
        return f"{self.explorer}/address/{address}" if self.explorer else None

    def tx_url(self, tx_hash: str) -> str | None:
        return f"{self.explorer}/tx/{tx_hash}" if self.explorer else None


BASE = Network(
    "base",
    8453,
    (
        "https://mainnet.base.org",
        "https://base-rpc.publicnode.com",
        "https://base.api.pocket.network",
    ),
    "https://basescan.org",
    2.0,
    "https://base.blockscout.com/api/",
)
BASE_SEPOLIA = Network(
    "base_sepolia",
    84532,
    ("https://sepolia.base.org", "https://base-sepolia-rpc.publicnode.com"),
    "https://sepolia.basescan.org",
    2.0,
    "https://base-sepolia.blockscout.com/api/",
)
ANVIL = Network("anvil", 31337, ("http://127.0.0.1:8545",), None, 1.0)

NETWORKS = {n.name: n for n in (BASE, BASE_SEPOLIA, ANVIL)}


def get(name: str) -> Network:
    """Look up a network by name (``base``, ``base-sepolia`` / ``base_sepolia``, ``anvil``)."""
    try:
        return NETWORKS[name.strip().lower().replace("-", "_")]
    except KeyError as e:
        raise ValueError(f"unknown network {name!r}; expected one of {sorted(NETWORKS)}") from e
