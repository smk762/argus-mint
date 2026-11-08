"""EIP-712 mint vouchers for the ``Card`` contract: hash, sign, recover. Pure; no I/O.

The consumer's server signs a voucher with a key that holds ``SIGNER_ROLE`` on the
contract; anyone (the player, or any relayer) can then submit it to ``Card.mint``.

    domain:  EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)
             name "argus-mint Card", version "1"
    struct:  MintVoucher(address to,bytes32 assetId,bytes32 contentHash,uint256 price,uint64 deadline)
"""

from __future__ import annotations

from typing import Any

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import to_checksum_address
from pydantic import BaseModel, ConfigDict, Field

from argus_mint._codec import UINT64_MAX, UINT256_MAX, Address, Bytes32, hex0x, keccak256

__all__ = [
    "DOMAIN_NAME",
    "DOMAIN_VERSION",
    "Domain",
    "MintVoucher",
    "domain_separator",
    "recover",
    "sign",
    "typed_data",
    "voucher_digest",
]

DOMAIN_NAME = "argus-mint Card"
DOMAIN_VERSION = "1"

DOMAIN_TYPE = "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
VOUCHER_TYPE = "MintVoucher(address to,bytes32 assetId,bytes32 contentHash,uint256 price,uint64 deadline)"
DOMAIN_TYPEHASH = keccak256(DOMAIN_TYPE.encode())
VOUCHER_TYPEHASH = keccak256(VOUCHER_TYPE.encode())


class Domain(BaseModel):
    """The EIP-712 domain of one deployed ``Card`` contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chain_id: int = Field(gt=0, le=UINT256_MAX)
    verifying_contract: Address
    name: str = DOMAIN_NAME
    version: str = DOMAIN_VERSION


class MintVoucher(BaseModel):
    """Permission to mint one card for ``asset_id`` to ``to``, for ``price`` wei, until ``deadline``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    to: Address
    asset_id: Bytes32  # consumer's id for the asset; each mints once
    content_hash: Bytes32  # metadata.content_hash(doc): image + provenance
    price: int = Field(ge=0, le=UINT256_MAX)  # wei; 0 for free mints
    deadline: int = Field(ge=0, le=UINT64_MAX)  # unix seconds


def domain_separator(domain: Domain) -> bytes:
    return keccak256(
        abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                DOMAIN_TYPEHASH,
                keccak256(domain.name.encode()),
                keccak256(domain.version.encode()),
                domain.chain_id,
                domain.verifying_contract,
            ],
        )
    )


def struct_hash(v: MintVoucher) -> bytes:
    return keccak256(
        abi_encode(
            ["bytes32", "address", "bytes32", "bytes32", "uint256", "uint64"],
            [VOUCHER_TYPEHASH, v.to, v.asset_id, v.content_hash, v.price, v.deadline],
        )
    )


def voucher_digest(v: MintVoucher, domain: Domain) -> bytes:
    """The 32-byte EIP-712 digest the signer signs and ``Card`` recovers from."""
    return keccak256(b"\x19\x01" + domain_separator(domain) + struct_hash(v))


def sign(v: MintVoucher, domain: Domain, key: LocalAccount) -> bytes:
    """65-byte signature ``r ‖ s ‖ v`` (v = 27/28, low-s), as OpenZeppelin ``ECDSA`` expects."""
    return bytes(key.unsafe_sign_hash(voucher_digest(v, domain)).signature)


def recover(v: MintVoucher, domain: Domain, sig: bytes) -> str:
    """Checksummed address that signed this voucher (any well-formed sig recovers *some* address)."""
    if len(sig) != 65:
        raise ValueError(f"expected a 65-byte signature, got {len(sig)}")
    return to_checksum_address(Account._recover_hash(voucher_digest(v, domain), signature=sig))


def typed_data(v: MintVoucher, domain: Domain) -> dict[str, Any]:
    """The voucher as EIP-712 typed data (for ``eth_signTypedData_v4`` wallets and tooling)."""
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "MintVoucher": [
                {"name": "to", "type": "address"},
                {"name": "assetId", "type": "bytes32"},
                {"name": "contentHash", "type": "bytes32"},
                {"name": "price", "type": "uint256"},
                {"name": "deadline", "type": "uint64"},
            ],
        },
        "primaryType": "MintVoucher",
        "domain": {
            "name": domain.name,
            "version": domain.version,
            "chainId": domain.chain_id,
            "verifyingContract": domain.verifying_contract,
        },
        "message": {
            "to": v.to,
            "assetId": hex0x(v.asset_id),
            "contentHash": hex0x(v.content_hash),
            "price": v.price,
            "deadline": v.deadline,
        },
    }
