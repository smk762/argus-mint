"""Record a review decision in a template, frame or bio template file.

Approval is a human step recorded in the file (hard rule 2): ``status: approved`` plus
``approved_by`` and ``approved_at``. This module writes exactly that, editing the YAML as
text so the file's comments and layout survive, then re-validates the result and restores
the original if it doesn't load. Only two moves exist: ``draft -> approved`` and
``approved -> retired``. A retired file keeps its approval lines, so its hash, which cards
already carry, doesn't change.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml

__all__ = ["CatalogueError", "record_decision"]

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
    doc = yaml.safe_load(original)
    status = doc.get("status") if isinstance(doc, dict) else None
    if decision == "approve" and status != "draft":
        raise CatalogueError(f"only a draft can be approved; this one is {status}")
    if decision == "retire" and status != "approved":
        raise CatalogueError(f"only an approved file can be retired; this one is {status}")
    if not by.strip() or "\n" in by:
        raise CatalogueError("the reviewer's name is required, on one line")

    new_status = "approved" if decision == "approve" else "retired"
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
    path.write_text(text, encoding="utf-8")
    try:
        validate(path)
    except Exception as e:
        path.write_text(original, encoding="utf-8")
        raise CatalogueError(f"{path.name} did not validate after the edit, so it was left unchanged: {e}") from e
    return new_status
