"""The committed Python-built fixtures (consumed by the forge tests) must match the code."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_python_fixtures_are_current():
    spec = importlib.util.spec_from_file_location("py_fixtures", ROOT / "scripts" / "py_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name, text in mod.render().items():
        committed = (ROOT / "tests" / "fixtures" / name).read_text()
        assert committed == text, f"tests/fixtures/{name} is stale; run `make fixtures`"
