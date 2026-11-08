"""Copy each contract's ABI from contracts/out into src/argus_mint/abi/.

The ABIs are committed so a Python-only install never needs Foundry; CI re-runs this
after `forge build` and fails if the committed copies drift. Stdlib only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "contracts" / "src"
OUT = ROOT / "contracts" / "out"
DEST = ROOT / "src" / "argus_mint" / "abi"


def main() -> int:
    names = sorted(p.stem for p in SRC.glob("*.sol"))
    for name in names:
        artifact = OUT / f"{name}.sol" / f"{name}.json"
        if not artifact.exists():
            print(f"missing {artifact}; run `forge build` first", file=sys.stderr)
            return 1
        abi = json.loads(artifact.read_text())["abi"]
        (DEST / f"{name}.json").write_text(json.dumps(abi, indent=2, sort_keys=True) + "\n")
        print(f"wrote abi/{name}.json ({len(abi)} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
