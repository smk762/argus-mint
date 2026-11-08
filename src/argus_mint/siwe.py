"""Sign-In with Ethereum (EIP-4361): build, parse and verify sign-in messages.

Pure apart from :func:`make_nonce` (randomness). The consumer issues a nonce, stores it,
and checks it is unused when the signed message comes back; argus-mint keeps no state.
Verification is for EOAs (ECDSA personal_sign); contract wallets (EIP-1271) need an RPC
call and are out of scope for v1.

Time fields are kept as the exact RFC 3339 strings in the message, so :meth:`SiweMessage.text`
reproduces the signed text byte for byte; :func:`parse` rejects any message that doesn't.
"""

from __future__ import annotations

import re
import secrets
import string
from datetime import UTC, datetime, timedelta

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import is_checksum_address, to_checksum_address
from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["DEFAULT_TTL", "SiweError", "SiweMessage", "create_message", "make_nonce", "parse", "verify"]

DEFAULT_TTL = timedelta(minutes=10)
_PREAMBLE = " wants you to sign in with your Ethereum account:"
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")
_NONCE = re.compile(r"^[A-Za-z0-9]{8,}$")
_DOMAIN = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$")
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:\S+$")


class SiweError(ValueError):
    """A sign-in message is malformed, stale, for another site, or not signed by its address."""


def _check_time(v: str | None) -> str | None:
    if v is None:
        return v
    if not _RFC3339.match(v):
        raise ValueError(f"not an RFC 3339 date-time: {v!r}")
    datetime.fromisoformat(v)
    return v


def _to_rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware")
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class SiweMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    domain: str
    address: str
    uri: str
    chain_id: int = Field(gt=0)
    nonce: str
    issued_at: str
    statement: str | None = None
    scheme: str | None = None
    version: str = "1"
    expiration_time: str | None = None
    not_before: str | None = None
    request_id: str | None = None
    resources: tuple[str, ...] = ()

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: str) -> str:
        if not _DOMAIN.match(v):
            raise ValueError(f"not a host[:port] authority: {v!r}")
        return v

    @field_validator("scheme")
    @classmethod
    def _scheme(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*$", v):
            raise ValueError(f"bad scheme {v!r}")
        return v

    @field_validator("address")
    @classmethod
    def _address(cls, v: str) -> str:
        if not is_checksum_address(v):
            raise ValueError("address must be EIP-55 checksummed")
        return v

    @field_validator("uri")
    @classmethod
    def _uri(cls, v: str) -> str:
        if not _URI.match(v):
            raise ValueError(f"not a URI: {v!r}")
        return v

    @field_validator("resources")
    @classmethod
    def _resources(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for r in v:
            if not _URI.match(r):
                raise ValueError(f"resource is not a URI: {r!r}")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if v != "1":
            raise ValueError("only SIWE version 1 is defined")
        return v

    @field_validator("nonce")
    @classmethod
    def _nonce(cls, v: str) -> str:
        if not _NONCE.match(v):
            raise ValueError("nonce must be at least 8 alphanumeric characters")
        return v

    @field_validator("statement", "request_id")
    @classmethod
    def _single_line(cls, v: str | None) -> str | None:
        if v is not None and ("\n" in v or "\r" in v):
            raise ValueError("must be a single line")
        return v

    @field_validator("issued_at", "expiration_time", "not_before")
    @classmethod
    def _times(cls, v: str | None) -> str | None:
        return _check_time(v)

    def text(self) -> str:
        """The EIP-4361 message text a wallet signs."""
        origin = f"{self.scheme}://{self.domain}" if self.scheme else self.domain
        lines = [origin + _PREAMBLE, self.address, ""]
        if self.statement is not None:
            lines.append(self.statement)
        lines += [
            "",
            f"URI: {self.uri}",
            f"Version: {self.version}",
            f"Chain ID: {self.chain_id}",
            f"Nonce: {self.nonce}",
            f"Issued At: {self.issued_at}",
        ]
        if self.expiration_time is not None:
            lines.append(f"Expiration Time: {self.expiration_time}")
        if self.not_before is not None:
            lines.append(f"Not Before: {self.not_before}")
        if self.request_id is not None:
            lines.append(f"Request ID: {self.request_id}")
        if self.resources:
            lines.append("Resources:")
            lines += [f"- {r}" for r in self.resources]
        return "\n".join(lines)


def make_nonce(length: int = 22) -> str:
    """A random alphanumeric nonce (22 chars ≈ 131 bits). Store it; accept it once."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_message(
    *,
    domain: str,
    address: str,
    uri: str,
    chain_id: int,
    nonce: str,
    issued_at: datetime,
    ttl: timedelta | None = DEFAULT_TTL,
    statement: str | None = None,
    scheme: str | None = None,
    request_id: str | None = None,
    resources: tuple[str, ...] = (),
) -> SiweMessage:
    """A message for ``address`` (any case; it is checksummed) expiring ``ttl`` after ``issued_at``."""
    return SiweMessage(
        domain=domain,
        address=to_checksum_address(address),
        uri=uri,
        chain_id=chain_id,
        nonce=nonce,
        issued_at=_to_rfc3339(issued_at),
        expiration_time=_to_rfc3339(issued_at + ttl) if ttl is not None else None,
        statement=statement,
        scheme=scheme,
        request_id=request_id,
        resources=resources,
    )


_FIELDS = {
    "URI": "uri",
    "Version": "version",
    "Chain ID": "chain_id",
    "Nonce": "nonce",
    "Issued At": "issued_at",
    "Expiration Time": "expiration_time",
    "Not Before": "not_before",
    "Request ID": "request_id",
}


def parse(text: str) -> SiweMessage:
    """Parse an EIP-4361 message. Raises :class:`SiweError` unless it round-trips exactly."""
    lines = text.split("\n")
    try:
        if not lines[0].endswith(_PREAMBLE):
            raise SiweError("missing the sign-in preamble")
        origin = lines[0][: -len(_PREAMBLE)]
        scheme, _, domain = origin.rpartition("://")
        fields: dict[str, object] = {"domain": domain, "scheme": scheme or None, "address": lines[1]}
        if lines[2] != "":
            raise SiweError("expected a blank line after the address")
        i = 3
        if lines[i] != "":
            fields["statement"] = lines[i]
            i += 1
        if lines[i] != "":
            raise SiweError("expected a blank line before URI")
        i += 1
        while i < len(lines) and lines[i] != "Resources:":
            key, sep, value = lines[i].partition(": ")
            if not sep or key not in _FIELDS or _FIELDS[key] in fields:
                raise SiweError(f"unexpected line: {lines[i]!r}")
            fields[_FIELDS[key]] = value
            i += 1
        if i < len(lines):
            res = lines[i + 1 :]
            if not res or not all(r.startswith("- ") for r in res):
                raise SiweError("malformed Resources list")
            fields["resources"] = tuple(r[2:] for r in res)
        msg = SiweMessage.model_validate(fields)
    except IndexError as e:
        raise SiweError("message is truncated") from e
    except ValueError as e:  # includes pydantic.ValidationError
        if isinstance(e, SiweError):
            raise
        raise SiweError(str(e)) from e
    if msg.text() != text:
        raise SiweError("message is not in canonical EIP-4361 form (fields out of order or extra whitespace)")
    return msg


def verify(
    text: str,
    signature: bytes,
    *,
    domain: str,
    nonce: str,
    now: datetime,
    chain_id: int | None = None,
    scheme: str | None = None,
) -> SiweMessage:
    """Check a signed sign-in message and return it.

    Checks, in order: well-formed; ``domain`` (and ``scheme``/``chain_id`` if given) match;
    ``nonce`` is the one the server issued; ``now`` is within [not_before, expiration_time);
    the signature recovers to the message's address. The caller must also mark the nonce used.
    """
    msg = parse(text)
    if msg.domain != domain:
        raise SiweError(f"message is for {msg.domain!r}, not {domain!r}")
    if scheme is not None and msg.scheme != scheme:
        raise SiweError(f"message scheme is {msg.scheme!r}, not {scheme!r}")
    if chain_id is not None and msg.chain_id != chain_id:
        raise SiweError(f"message is for chain {msg.chain_id}, not {chain_id}")
    if not secrets.compare_digest(msg.nonce, nonce):
        raise SiweError("nonce does not match")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if msg.expiration_time is not None and now >= datetime.fromisoformat(msg.expiration_time):
        raise SiweError("message has expired")
    if msg.not_before is not None and now < datetime.fromisoformat(msg.not_before):
        raise SiweError("message is not valid yet")
    try:
        signer = Account.recover_message(encode_defunct(text=text), signature=signature)
    except Exception as e:  # malformed signature bytes
        raise SiweError(f"bad signature: {e}") from e
    if signer != msg.address:
        raise SiweError("signature is not from the message's address")
    return msg
