"""cards.write_atomic: unique temp files, fsync before rename, no leftovers on failure."""

from __future__ import annotations

import os

import pytest

from argus_mint.cards import write_atomic


def test_each_writer_gets_its_own_temp_file(tmp_path):
    # A fixed ".name.tmp" is shared by every writer of the same path: occupy it and the write must still work.
    (tmp_path / ".draft.json.tmp").mkdir()
    write_atomic(tmp_path / "draft.json", "new")
    assert (tmp_path / "draft.json").read_text() == "new"


def test_a_failed_write_leaves_no_temp_file_and_keeps_the_old_one(tmp_path):
    path = tmp_path / "draft.json"
    path.write_text("old")
    with pytest.raises(UnicodeEncodeError):
        write_atomic(path, "bad \udcff surrogate")
    assert path.read_text() == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["draft.json"]


def test_the_data_is_fsynced_before_the_rename(tmp_path, monkeypatch):
    path = tmp_path / "draft.json"
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1])
    write_atomic(path, "new")
    assert events[:2] == ["fsync", "replace"]
    assert path.read_text() == "new"
