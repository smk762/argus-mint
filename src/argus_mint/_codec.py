"""Shared encoding helpers: keccak256, canonical JSON, and Pydantic field types for
32-byte values and addresses. Pure; no I/O."""

from __future__ import annotations

import json
from typing import Annotated, Any

from Crypto.Hash import keccak as _keccak
from eth_utils import is_address, to_checksum_address
from pydantic import BeforeValidator, PlainSerializer


def keccak256(data: bytes) -> bytes:
    """Ethereum's keccak256 (not NIST SHA3-256)."""
    return _keccak.new(digest_bits=256, data=data).digest()


def canonical(doc: Any) -> bytes:
    """Canonical JSON: sorted keys, compact separators, UTF-8, no NaN/Infinity.

    The same document always gives the same bytes, so its hash is stable.
    """
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def to_bytes32(value: Any) -> bytes:
    """Accept 32 raw bytes or a 0x-prefixed 64-hex-digit string."""
    if isinstance(value, bytes | bytearray):
        b = bytes(value)
    elif isinstance(value, str):
        if not value.startswith("0x") or len(value) != 66:
            raise ValueError("expected 0x followed by 64 hex digits")
        try:
            b = bytes.fromhex(value[2:])
        except ValueError as e:
            raise ValueError("expected 0x followed by 64 hex digits") from e
    else:
        raise ValueError("expected 32 bytes or a 0x hex string")
    if len(b) != 32:
        raise ValueError(f"expected 32 bytes, got {len(b)}")
    return b


def hex0x(b: bytes) -> str:
    return "0x" + b.hex()


def to_address(value: Any) -> str:
    """Validate an address and return its EIP-55 checksum form.

    A mixed-case address with a wrong checksum is rejected (it's probably a typo).
    """
    if not isinstance(value, str) or not is_address(value):
        raise ValueError(f"not a valid address: {value!r}")
    return to_checksum_address(value)


Bytes32 = Annotated[bytes, BeforeValidator(to_bytes32), PlainSerializer(hex0x, return_type=str, when_used="json")]
Address = Annotated[str, BeforeValidator(to_address)]

UINT64_MAX = 2**64 - 1
UINT256_MAX = 2**256 - 1
