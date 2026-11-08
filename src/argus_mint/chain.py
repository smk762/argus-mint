"""Shared web3 plumbing for the chain clients (the ``chain`` extra): bind a committed ABI
to an address, and send a contract call as a transaction and wait for it to land."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from argus_mint import abi, networks
from argus_mint.networks import Network

try:
    from web3 import Web3
    from web3.contract import Contract
except ImportError as e:  # pragma: no cover - exercised only without the extra
    raise ImportError("the chain clients need the `chain` extra: pip install 'argus-mint[chain]'") from e

__all__ = ["NoRpcAvailable", "TxFailed", "connect", "contract", "send"]


class TxFailed(Exception):
    """A transaction was mined but reverted."""


class NoRpcAvailable(Exception):
    """None of the RPC endpoints answered with the expected chain id."""


def connect(network: Network | str, urls: Sequence[str] | None = None, *, timeout: float = 10) -> Web3:
    """A ``Web3`` on the first endpoint that answers with the network's chain id.

    ``urls`` defaults to the network's public endpoints; pass your provider's URL first to
    prefer it. An endpoint on the wrong chain is skipped, never used.
    """
    net = networks.get(network) if isinstance(network, str) else network
    errors: list[str] = []
    for url in urls or net.public_rpcs:
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": timeout}))
        try:
            chain_id = w3.eth.chain_id
        except Exception as e:  # unreachable, rate-limited, TLS: try the next one
            errors.append(f"{url}: {e}")
            continue
        if chain_id == net.chain_id:
            return w3
        errors.append(f"{url}: chain id {chain_id}, expected {net.chain_id}")
    raise NoRpcAvailable(f"no RPC for {net.name}: " + "; ".join(errors))


def contract(w3: Web3, name: str, address: str) -> Contract:
    """A web3 contract for ``Card``, ``Deed`` or ``Ledger`` at ``address``."""
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi.load(name))


def send(w3: Web3, fn: Any, account: Any = None, *, timeout: float = 120) -> Any:
    """Send a contract function call and return its receipt.

    ``account`` is a ``LocalAccount`` (the tx is signed locally) or an address the node
    holds unlocked (dev chains); ``None`` uses ``w3.eth.default_account``.
    """
    if account is not None and hasattr(account, "sign_transaction"):
        tx = fn.build_transaction(
            {"from": account.address, "nonce": w3.eth.get_transaction_count(account.address, "pending")}
        )
        tx_hash = w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)
    else:
        sender = account or w3.eth.default_account
        tx_hash = fn.transact({"from": sender} if sender else {})
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    if receipt.status != 1:
        raise TxFailed(f"transaction {tx_hash.hex()} reverted")
    return receipt
