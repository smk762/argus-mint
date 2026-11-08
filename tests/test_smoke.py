from __future__ import annotations

import argus_mint


def test_version_is_exposed() -> None:
    assert isinstance(argus_mint.__version__, str)
    assert argus_mint.__version__
