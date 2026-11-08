"""Where signing keys come from: a Foundry keystore account, or a raw key in the environment.

A keystore is the encrypted JSON that ``cast wallet new <name>`` / ``cast wallet import``
write to ``~/.foundry/keystores/<name>`` (standard Web3 Secret Storage v3). Its password
comes from a file named by ``$ARGUS_MINT_KEYSTORE_PASSWORD_FILE`` (unattended servers: a
secret mount, mode 600) or, failing that, a hidden prompt on the terminal. The fallback is
``$ARGUS_MINT_SIGNER_KEY`` (a raw 0x key), for hosts whose secret store hands out env vars.

A key is never read from a command-line argument (that would leak into shell history and
``ps``).
"""

from __future__ import annotations

import getpass
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path

from eth_account import Account
from eth_account.signers.local import LocalAccount

__all__ = [
    "KEY_ENV",
    "KEYSTORE_DIR_ENV",
    "PASSWORD_FILE_ENV",
    "KeyLoadError",
    "keystore_path",
    "load_keystore",
    "load_signer",
]

KEY_ENV = "ARGUS_MINT_SIGNER_KEY"
PASSWORD_FILE_ENV = "ARGUS_MINT_KEYSTORE_PASSWORD_FILE"
KEYSTORE_DIR_ENV = "ARGUS_MINT_KEYSTORE_DIR"


class KeyLoadError(Exception):
    """A key could not be loaded (missing keystore, wrong password, malformed key)."""


def keystore_path(account: str, env: Mapping[str, str] = os.environ) -> Path:
    """``account`` as a path if it names a file, else ``<keystore dir>/<account>``.

    The keystore dir is ``$ARGUS_MINT_KEYSTORE_DIR`` or Foundry's ``~/.foundry/keystores``.
    """
    p = Path(account).expanduser()
    if os.sep in account or p.is_file():
        return p
    directory = Path(env.get(KEYSTORE_DIR_ENV) or Path.home() / ".foundry" / "keystores").expanduser()
    return directory / account


def load_keystore(path: Path, password: str) -> LocalAccount:
    try:
        keyfile = json.loads(path.read_text())
    except FileNotFoundError as e:
        raise KeyLoadError(f"no keystore at {path} (create one with `cast wallet new {path.name}`)") from e
    except (OSError, ValueError) as e:
        raise KeyLoadError(f"cannot read keystore {path}: {e}") from e
    try:
        return Account.from_key(Account.decrypt(keyfile, password))
    except ValueError as e:  # eth-account reports a wrong password as "MAC mismatch"
        raise KeyLoadError(f"cannot decrypt keystore {path}: wrong password?") from e


def load_signer(
    account: str | None,
    env: Mapping[str, str] = os.environ,
    prompt: Callable[[str], str] = getpass.getpass,
) -> LocalAccount | None:
    """The account to sign with: the keystore ``account`` if given, else ``$ARGUS_MINT_SIGNER_KEY``.

    Returns None when neither is set. ``prompt`` asks for the keystore password when no
    password file is configured (``getpass`` writes to the terminal, not stdout).
    """
    if account:
        path = keystore_path(account, env)
        if not path.is_file():
            raise KeyLoadError(f"no keystore at {path} (create one with `cast wallet new {account}`)")
        if pw_file := env.get(PASSWORD_FILE_ENV):
            try:
                password = Path(pw_file).expanduser().read_text().rstrip("\r\n")
            except OSError as e:
                raise KeyLoadError(f"cannot read ${PASSWORD_FILE_ENV}: {e}") from e
        else:
            password = prompt(f"password for keystore {path.name}: ")
        return load_keystore(path, password)
    if key := env.get(KEY_ENV):
        try:
            return Account.from_key(key.strip())
        except (ValueError, TypeError) as e:
            raise KeyLoadError(f"${KEY_ENV} is not a valid private key") from e
    return None
