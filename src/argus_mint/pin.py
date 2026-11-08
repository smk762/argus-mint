"""IPFS pinning: put bytes or canonical JSON on IPFS and get back the CID.

The pinning service is an open choice (self-hosted node or a provider), so callers
depend on the :class:`Pinner` protocol. Two implementations ship: :class:`KuboPinner`
for a self-hosted Kubo node's RPC API, and :class:`LocalPinner` for tests and dry runs.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any, Protocol

import httpx

from argus_mint._codec import canonical

__all__ = ["KuboPinner", "LocalPinner", "PinError", "Pinner", "raw_cid"]


class PinError(Exception):
    pass


class Pinner(Protocol):
    def pin_bytes(self, data: bytes, name: str) -> str: ...

    def pin_json(self, doc: dict[str, Any], name: str) -> str: ...


def raw_cid(data: bytes) -> str:
    """CIDv1 (raw codec, sha2-256, base32) of ``data`` as a single block.

    Equals what IPFS assigns a file of up to one chunk (256 KiB) added with
    ``--cid-version=1``; larger files get a DAG root instead, so treat this as a local id.
    """
    multihash = bytes([0x12, 0x20]) + hashlib.sha256(data).digest()
    cid = bytes([0x01, 0x55]) + multihash
    return "b" + base64.b32encode(cid).decode().lower().rstrip("=")


class LocalPinner:
    """In-memory pinner: deterministic CIDs, nothing leaves the process."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.names: dict[str, str] = {}

    def pin_bytes(self, data: bytes, name: str) -> str:
        cid = raw_cid(data)
        self.store[cid] = data
        self.names[cid] = name
        return cid

    def pin_json(self, doc: dict[str, Any], name: str) -> str:
        return self.pin_bytes(canonical(doc), name)


class KuboPinner:
    """Pins through a Kubo node's RPC API (``POST /api/v0/add``), CIDv1, pinned."""

    def __init__(
        self, api_url: str = "http://127.0.0.1:5001", *, client: httpx.Client | None = None, timeout: float = 60
    ):
        self.api_url = api_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout)

    def pin_bytes(self, data: bytes, name: str) -> str:
        try:
            resp = self.client.post(
                f"{self.api_url}/api/v0/add",
                params={"cid-version": "1", "pin": "true", "quieter": "true"},
                files={"file": (name, data, "application/octet-stream")},
            )
            resp.raise_for_status()
            cid = resp.json()["Hash"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise PinError(f"pinning {name!r} failed: {e}") from e
        if not isinstance(cid, str) or not cid:
            raise PinError(f"pinning {name!r}: node returned no CID")
        return cid

    def pin_json(self, doc: dict[str, Any], name: str) -> str:
        return self.pin_bytes(canonical(doc), name)
