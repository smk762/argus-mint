from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from argus_mint.siwe import SiweError, create_message, make_nonce, parse, verify

KEY = Account.from_key("0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d")
T0 = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
NONCE = "abcDEF123456"


def signed(msg_text: str, key=KEY) -> bytes:
    return bytes(key.sign_message(encode_defunct(text=msg_text)).signature)


def make(**kw):
    args = dict(
        domain="realms.example.org",
        address=KEY.address.lower(),
        uri="https://realms.example.org/login",
        chain_id=8453,
        nonce=NONCE,
        issued_at=T0,
        statement="Link your wallet to atlas-realms.",
    )
    args.update(kw)
    return create_message(**args)


def test_text_format():
    text = make().text()
    assert text == (
        "realms.example.org wants you to sign in with your Ethereum account:\n"
        f"{KEY.address}\n"
        "\n"
        "Link your wallet to atlas-realms.\n"
        "\n"
        "URI: https://realms.example.org/login\n"
        "Version: 1\n"
        "Chain ID: 8453\n"
        f"Nonce: {NONCE}\n"
        "Issued At: 2026-11-08T12:00:00Z\n"
        "Expiration Time: 2026-11-08T12:10:00Z"
    )


def test_no_statement_format():
    lines = make(statement=None).text().split("\n")
    assert lines[1:5] == [KEY.address, "", "", "URI: https://realms.example.org/login"]


def test_parse_round_trips_all_fields():
    msg = make(
        scheme="https",
        request_id="req-1",
        resources=("ipfs://bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi", "https://x.org/a"),
    )
    msg = msg.model_copy(update={"not_before": "2026-11-08T12:00:00Z"})
    assert parse(msg.text()) == msg
    assert parse(make(statement=None).text()) == make(statement=None)


def test_verify_ok():
    text = make().text()
    msg = verify(text, signed(text), domain="realms.example.org", nonce=NONCE, now=T0 + timedelta(minutes=1))
    assert msg.address == KEY.address


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"domain": "evil.example.org"}, "not 'evil"),
        ({"nonce": "otherNonce123"}, "nonce"),
        ({"now": T0 + timedelta(minutes=10)}, "expired"),
        ({"chain_id": 1}, "chain"),
    ],
)
def test_verify_rejections(kw, match):
    text = make().text()
    args = dict(domain="realms.example.org", nonce=NONCE, now=T0 + timedelta(minutes=1))
    args.update(kw)
    with pytest.raises(SiweError, match=match):
        verify(text, signed(text), **args)


def test_verify_not_before():
    msg = make().model_copy(update={"not_before": "2026-11-08T12:05:00Z"})
    text = msg.text()
    with pytest.raises(SiweError, match="not valid yet"):
        verify(text, signed(text), domain=msg.domain, nonce=NONCE, now=T0)


def test_verify_wrong_signer():
    text = make().text()
    other = Account.from_key("0x" + "11" * 32)
    with pytest.raises(SiweError, match="not from"):
        verify(text, signed(text, other), domain="realms.example.org", nonce=NONCE, now=T0)


def test_verify_garbage_signature():
    text = make().text()
    with pytest.raises(SiweError, match="bad signature"):
        verify(text, b"\x00" * 65, domain="realms.example.org", nonce=NONCE, now=T0)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.replace("Version: 1\n", ""),
        lambda t: t.replace("Nonce:", "Nonce: "),
        lambda t: t + "\n",
        lambda t: t.replace("Chain ID: 8453\nNonce", "Nonce").replace("Issued At", "Chain ID: 8453\nIssued At"),
        lambda t: t.lower(),
        lambda t: t.split("\n", 1)[0],
        lambda t: t.replace("Nonce: abcDEF123456", "Nonce: short"),
        lambda t: t.replace("2026-11-08T12:00:00Z", "yesterday"),
    ],
)
def test_parse_rejects_malformed(mutate):
    with pytest.raises(SiweError):
        parse(mutate(make().text()))


def test_make_nonce():
    n = make_nonce()
    assert len(n) == 22 and n.isalnum()
    assert make_nonce() != n


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        make(issued_at=datetime(2026, 1, 1))
