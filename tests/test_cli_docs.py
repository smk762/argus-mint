"""docs/cli.md is generated from the CLI; regenerate with ``make cli-docs`` when this fails."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("typer")

from argus_mint._cli_docs import render  # noqa: E402

DOCS = Path(__file__).resolve().parents[1] / "docs"


def test_cli_docs_are_current():
    assert (DOCS / "cli.md").read_text(encoding="utf-8") == render(), "docs/cli.md is stale: run `make cli-docs`"


def test_every_command_is_documented():
    text = render()
    for cmd in ("draft", "review", "publish", "serve", "library build", "library list"):
        assert f"## argus-mint {cmd}\n" in text
