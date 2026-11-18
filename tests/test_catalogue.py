"""catalogue.record_decision: the file is replaced atomically, and only by a version that validated."""

from __future__ import annotations

from datetime import date

import pytest
import yaml

from argus_mint.catalogue import CatalogueError, record_decision
from argus_mint.templates import load_template


def _draft(tmp_path, template_doc):
    doc = {k: v for k, v in template_doc.items() if k not in ("approved_by", "approved_at")} | {"status": "draft"}
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def test_the_original_is_never_written_in_place(tmp_path, template_doc, monkeypatch):
    path = _draft(tmp_path, template_doc)
    original = path.read_text()
    written: list[str] = []
    real = type(path).write_text
    monkeypatch.setattr(
        type(path), "write_text", lambda self, *a, **k: (written.append(self.name), real(self, *a, **k))[1]
    )
    assert record_decision(path, "approve", by="mod", today=date(2026, 11, 17), validate=load_template) == "approved"
    assert "t.yaml" not in written  # replaced by rename, never truncated and rewritten
    assert path.read_text() != original and "approved_by" in path.read_text()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["t.yaml"]


def test_a_failed_validation_leaves_the_file_untouched(tmp_path, template_doc):
    path = _draft(tmp_path, template_doc)
    original = path.read_text()
    stat = path.stat()

    def reject(p):
        raise ValueError("no")

    with pytest.raises(CatalogueError, match="left unchanged"):
        record_decision(path, "approve", by="mod", today=date(2026, 11, 17), validate=reject)
    assert path.read_text() == original and path.stat().st_ino == stat.st_ino
    assert sorted(p.name for p in tmp_path.iterdir()) == ["t.yaml"]
