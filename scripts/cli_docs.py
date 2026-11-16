"""Write docs/cli.md from the argus-mint CLI (``make cli-docs``). ``tests/test_cli_docs.py`` fails if it's stale."""

from pathlib import Path

from argus_mint._cli_docs import render

OUT = Path(__file__).resolve().parents[1] / "docs" / "cli.md"

if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(Path.cwd()) if OUT.is_relative_to(Path.cwd()) else OUT}")
