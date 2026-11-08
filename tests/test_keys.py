"""Signing-key sources: Foundry keystores (password file or prompt) and the raw-key env var."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from eth_account import Account

from argus_mint.keys import (
    KEY_ENV,
    KEYSTORE_DIR_ENV,
    PASSWORD_FILE_ENV,
    KeyLoadError,
    keystore_path,
    load_signer,
)

KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
ADDRESS = Account.from_key(KEY).address


def _no_prompt(_: str) -> str:
    raise AssertionError("prompted for a password")


@pytest.fixture
def keystores(tmp_path):
    d = tmp_path / "keystores"
    d.mkdir()
    (d / "deployer").write_text(json.dumps(Account.encrypt(KEY, "hunter2", iterations=2)))
    return d


def test_keystore_by_name_with_password_file(keystores, tmp_path):
    pw = tmp_path / "pw"
    pw.write_text("hunter2\n")  # a trailing newline (echo > file) is not part of the password
    env = {KEYSTORE_DIR_ENV: str(keystores), PASSWORD_FILE_ENV: str(pw)}
    assert load_signer("deployer", env, prompt=_no_prompt).address == ADDRESS


def test_keystore_by_path_with_prompt(keystores):
    asked = []
    acct = load_signer(str(keystores / "deployer"), {}, prompt=lambda msg: asked.append(msg) or "hunter2")
    assert acct.address == ADDRESS
    assert asked == ["password for keystore deployer: "]


def test_keystore_beats_env_key(keystores):
    other = Account.create().key.hex()
    env = {KEYSTORE_DIR_ENV: str(keystores), KEY_ENV: other}
    assert load_signer("deployer", env, prompt=lambda _: "hunter2").address == ADDRESS


def test_wrong_password(keystores):
    with pytest.raises(KeyLoadError, match="wrong password"):
        load_signer("deployer", {KEYSTORE_DIR_ENV: str(keystores)}, prompt=lambda _: "nope")


def test_missing_keystore(keystores):
    with pytest.raises(KeyLoadError, match="cast wallet new ghost"):
        load_signer("ghost", {KEYSTORE_DIR_ENV: str(keystores)}, prompt=_no_prompt)


def test_unreadable_password_file(keystores, tmp_path):
    env = {KEYSTORE_DIR_ENV: str(keystores), PASSWORD_FILE_ENV: str(tmp_path / "absent")}
    with pytest.raises(KeyLoadError, match=PASSWORD_FILE_ENV):
        load_signer("deployer", env, prompt=_no_prompt)


def test_env_key_and_none():
    assert load_signer(None, {KEY_ENV: KEY}).address == ADDRESS
    assert load_signer(None, {}) is None
    with pytest.raises(KeyLoadError, match="not a valid"):
        load_signer(None, {KEY_ENV: "0x1234"})


def test_default_dir_is_foundrys():
    assert keystore_path("deployer", {}) == Path.home() / ".foundry" / "keystores" / "deployer"


def test_reads_a_keystore_written_by_cast(tmp_path):
    cast = shutil.which("cast") or str(Path.home() / ".foundry" / "bin" / "cast")
    if not Path(cast).exists():
        pytest.skip("cast not installed")
    subprocess.run(
        [cast, "wallet", "import", "made-by-cast", "--private-key", KEY, "--keystore-dir", str(tmp_path),
         "--unsafe-password", "pw"],
        check=True, capture_output=True,
    )  # fmt: skip
    assert load_signer(str(tmp_path / "made-by-cast"), {}, prompt=lambda _: "pw").address == ADDRESS
