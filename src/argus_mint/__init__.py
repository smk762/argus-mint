"""argus-mint — provenance tokens for game assets.

Render from reviewed templates, pin to IPFS, mint ERC-721 cards and soulbound deeds,
and anchor commitments and Merkle roots on-chain. No game logic lives here.
"""

from __future__ import annotations

try:
    # Written by hatch-vcs at build time (see pyproject [tool.hatch.build.hooks.vcs]).
    from argus_mint._version import __version__
except ImportError:  # running from a source checkout that hasn't been built
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("argus-mint")
    except PackageNotFoundError:
        __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
