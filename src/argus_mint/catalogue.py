"""Record a review decision in a catalogue file: a template, frame or bio template, or a teaser
project's files (YAML, and the JSON teaser links).

Approval is a human step recorded in the file (hard rule 2): ``status: approved`` plus
``approved_by`` and ``approved_at``. This module writes exactly that, editing the YAML as
text so the file's comments and layout survive. The edited text is validated in a temp file
first and only then renamed over the original, which is never left half-written. Only two moves exist: ``draft -> approved`` and
``approved -> retired``. A retired file keeps its approval lines, so its hash, which cards
already carry, doesn't change.

A JSON file has no comments or layout to keep, so it is rewritten from its parsed content.
:func:`record_guard` writes a project file's SingGuard verdicts (its ``guard`` block) the same
way: validated in a temp file, then renamed over the original.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml

__all__ = ["CatalogueError", "record_decision", "record_guard"]

Decision = Literal["approve", "retire"]
_TOP = r"^{key}:[^\n]*\n"


class CatalogueError(ValueError):
    """The decision doesn't apply to the file's current status, or the file can't be updated."""


def _set_key(text: str, key: str, value: str, after: str) -> str:
    line = f"{key}: {value}\n"
    pattern = re.compile(_TOP.format(key=re.escape(key)), re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda _m: line, text, count=1)
    anchor = re.compile(_TOP.format(key=re.escape(after)), re.MULTILINE).search(text)
    if anchor is None:
        raise CatalogueError(f"no top-level `{after}:` line to put `{key}` after")
    return text[: anchor.end()] + line + text[anchor.end() :]


def _quote(s: str) -> str:
    return yaml.safe_dump(s, default_style='"', width=float("inf")).strip()  # one line, whatever its length


def record_decision(path: Path, decision: Decision, *, by: str, today: date, validate: Callable[[Path], Any]) -> str:
    """Write ``decision`` into the file at ``path``; return its new status.

    ``validate`` loads the file (``load_template``, ``load_frame``, ``load_bio``) and raises
    if it is invalid; on failure the original text is put back and :class:`CatalogueError`
    raised.
    """
    path = Path(path)
    original = path.read_text(encoding="utf-8")
    doc = json.loads(original) if path.suffix == ".json" else yaml.safe_load(original)
    status = doc.get("status") if isinstance(doc, dict) else None
    if decision == "approve" and status != "draft":
        raise CatalogueError(f"only a draft can be approved; this one is {status}")
    if decision == "retire" and status != "approved":
        raise CatalogueError(f"only an approved file can be retired; this one is {status}")
    if not by.strip() or "\n" in by:
        raise CatalogueError("the reviewer's name is required, on one line")

    new_status = "approved" if decision == "approve" else "retired"
    if path.suffix == ".json":
        doc["status"] = new_status
        if decision == "approve":
            doc["approved_by"] = by.strip()
            doc["approved_at"] = today.isoformat()
        _replace_validated(path, _json_text(doc), validate)
        return new_status
    text, found = re.subn(
        r"^status:[ \t]*\w+",
        f"status: {new_status}",
        original,
        count=1,
        flags=re.MULTILINE,
    )
    if not found:
        raise CatalogueError("no top-level `status: <word>` line to update (write the status unquoted)")
    if decision == "approve":
        text = _set_key(text, "approved_by", _quote(by.strip()), after="status")
        text = _set_key(text, "approved_at", today.isoformat(), after="approved_by")
    _replace_validated(path, text, validate)
    return new_status


def _json_text(doc: Any) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


# A top-level `guard:` key and its block: the indented lines, sequence items and blank lines under it.
_GUARD_BLOCK = re.compile(r"^guard:[^\n]*\n(?:(?:[ \t-][^\n]*)?\n)*", re.MULTILINE)


def record_guard(path: Path, verdicts: list[dict[str, Any]], *, validate: Callable[[Path], Any]) -> None:
    """Replace the file's ``guard`` block with ``verdicts`` (JSON-ready dicts); none removes it.

    YAML is edited as text, so the rest of the file (comments included) is kept.
    """
    path = Path(path)
    original = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        doc = json.loads(original)
        doc.pop("guard", None)
        if verdicts:
            doc["guard"] = verdicts
        _replace_validated(path, _json_text(doc), validate)
        return
    text = _GUARD_BLOCK.sub("", original).rstrip("\n") + "\n"
    if verdicts:
        text += yaml.safe_dump({"guard": verdicts}, sort_keys=False, allow_unicode=True, width=float("inf"))
    _replace_validated(path, text, validate)


def _replace_validated(path: Path, text: str, validate: Callable[[Path], Any]) -> None:
    # The edit goes to a temp file beside the original (so relative paths in it resolve the same; a
    # ``.tmp`` suffix, so a registry scanning for *.yaml never sees it), is validated there, and only then renamed over the original: the file is never half-written.
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            validate(tmp)
        except Exception as e:
            raise CatalogueError(f"{path.name} did not validate after the edit, so it was left unchanged: {e}") from e
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
