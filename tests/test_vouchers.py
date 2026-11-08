from __future__ import annotations

import pytest
from eth_account import Account
from eth_account.messages import _hash_eip191_message, encode_typed_data

from argus_mint.vouchers import (
    DOMAIN_NAME,
    Domain,
    MintVoucher,
    recover,
    sign,
    typed_data,
    voucher_digest,
)

# anvil's first default account (well-known test key, never holds real funds)
ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
SIGNER = Account.from_key(ANVIL_KEY)
DOMAIN = Domain(chain_id=31337, verifying_contract="0x5FbDB2315678afecb367f032d93F642f64180aa3")
VOUCHER = MintVoucher(
    to="0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
    asset_id="0x" + "01" * 32,
    content_hash="0x" + "02" * 32,
    price=10**15,
    deadline=1_800_000_000,
)


def test_domain_defaults():
    assert DOMAIN.name == DOMAIN_NAME == "argus-mint Card"
    assert DOMAIN.version == "1"


def test_digest_matches_eth_account_typed_data():
    signable = encode_typed_data(full_message=typed_data(VOUCHER, DOMAIN))
    assert voucher_digest(VOUCHER, DOMAIN) == _hash_eip191_message(signable)


def test_sign_and_recover():
    sig = sign(VOUCHER, DOMAIN, SIGNER)
    assert len(sig) == 65 and sig[-1] in (27, 28)
    assert recover(VOUCHER, DOMAIN, sig) == SIGNER.address


def test_signature_matches_wallet_sign_typed_data():
    wallet_sig = Account.sign_typed_data(ANVIL_KEY, full_message=typed_data(VOUCHER, DOMAIN)).signature
    assert sign(VOUCHER, DOMAIN, SIGNER) == bytes(wallet_sig)


@pytest.mark.parametrize(
    "change",
    [{"price": 0}, {"deadline": 1}, {"asset_id": "0x" + "ff" * 32}, {"to": "0x" + "33" * 20}],
)
def test_any_field_change_breaks_the_signature(change):
    sig = sign(VOUCHER, DOMAIN, SIGNER)
    changed = MintVoucher.model_validate({**VOUCHER.model_dump(), **change})
    assert recover(changed, DOMAIN, sig) != SIGNER.address


def test_domain_binds_chain_and_contract():
    sig = sign(VOUCHER, DOMAIN, SIGNER)
    assert recover(VOUCHER, DOMAIN.model_copy(update={"chain_id": 8453}), sig) != SIGNER.address
    other = Domain.model_validate({**DOMAIN.model_dump(), "verifying_contract": "0x" + "44" * 20})
    assert recover(VOUCHER, other, sig) != SIGNER.address


def test_validation():
    with pytest.raises(ValueError):
        MintVoucher.model_validate({**VOUCHER.model_dump(), "price": -1})
    with pytest.raises(ValueError):
        MintVoucher.model_validate({**VOUCHER.model_dump(), "deadline": 2**64})
    with pytest.raises(ValueError):
        MintVoucher.model_validate({**VOUCHER.model_dump(), "to": "0xnotanaddress"})
    with pytest.raises(ValueError):
        recover(VOUCHER, DOMAIN, b"\x00" * 64)


def test_addresses_are_checksummed():
    v = MintVoucher.model_validate({**VOUCHER.model_dump(), "to": VOUCHER.to.lower()})
    assert v.to == VOUCHER.to


def test_json_round_trip():
    again = MintVoucher.model_validate_json(VOUCHER.model_dump_json())
    assert again == VOUCHER
    assert '"0x0101' in VOUCHER.model_dump_json()
