"""requirements-test.txt (a plain-pip test environment) pins what uv.lock pins."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_requirements_test_matches_the_lock():
    locked = {_norm(p["name"]): p.get("version") for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"]}
    pins = {}
    for line in (ROOT / "requirements-test.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, _, version = line.partition("==")
            pins[_norm(name.split("[", 1)[0])] = version
    assert pins, "requirements-test.txt pins nothing"
    stale = {n: (v, locked.get(n)) for n, v in pins.items() if locked.get(n) != v}
    assert not stale, f"requirements-test.txt differs from uv.lock (pinned, locked): {stale}"


def test_requirements_test_covers_every_dependency():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    wanted = list(project["dependencies"])
    for extra in ("chain", "cli", "frames", "server", "dev"):
        wanted += [d for d in project["optional-dependencies"][extra] if not d.startswith("argus-mint")]
    names = {_norm(re.split(r"[\[<>=!~ ]", d, maxsplit=1)[0]) for d in wanted} - {"ruff"}  # lint runs via uvx
    pinned = {
        _norm(re.split(r"[\[=]", line.strip(), maxsplit=1)[0])
        for line in (ROOT / "requirements-test.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert names <= pinned, f"missing from requirements-test.txt: {sorted(names - pinned)}"
