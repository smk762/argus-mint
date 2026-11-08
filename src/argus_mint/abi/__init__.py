"""Contract ABIs, copied from the Foundry build by `make abi` and committed, so a
Python-only install never needs Foundry. CI fails if they drift from the contracts."""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import Any

NAMES = ("Card", "Deed", "Ledger")


@cache
def load(name: str) -> list[dict[str, Any]]:
    """The ABI of ``Card``, ``Deed`` or ``Ledger``."""
    if name not in NAMES:
        raise ValueError(f"unknown contract {name!r}; expected one of {NAMES}")
    return json.loads(files(__name__).joinpath(f"{name}.json").read_text())
