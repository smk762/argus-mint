"""GPU leases for work argus-mint asks for directly: the bio writer's model.

Renders go through darkroom, which takes its own leases (mint only tells it the priority). A bio is written by a
model in Ollama behind the suite's chat API, so the writer takes a lease on ``ollama:<model>`` first. The coordinator
comes from argus-cortex's environment: ``ARGUS_GPU_BROKER_URL`` (with ``ARGUS_GPU_BROKER_TOKEN``) alone switches the
argus-iris broker on, and cortex reads the tenant from ``gpu-tenant.yaml`` in the working directory or at
``/app/gpu-tenant.yaml``, where the image ships it. Without argus-cortex installed (a base or ``cli`` install), or with
no broker URL (or ``ARGUS_GPU_COORDINATOR=none``), nothing is leased. ``argus-mint serve`` registers the tenant at
startup (:func:`keep_registered`); the CLI registers with its first lease.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

__all__ = ["BIO_KIND", "BIO_MODEL_MB", "BIO_VRAM_MB", "coordinator", "keep_registered"]

# The bio kind declared in gpu-tenant.yaml (tests/test_gpu.py keeps them equal). MiB, for mistral-nemo 12B (Q4):
# the broker uses its learned p95 when larger, so a heavier bio model is caught after a few leases.
BIO_KIND = "bio"
BIO_VRAM_MB = 8500
BIO_MODEL_MB = 7100


def coordinator() -> Any | None:
    """The GPU coordinator the environment names, or ``None`` (no argus-cortex, no ``ARGUS_GPU_BROKER_URL``, or
    ``ARGUS_GPU_COORDINATOR=none``)."""
    try:
        from argus_cortex.gpu import coordinator_from_env  # noqa: PLC0415 - optional, in the server extra
    except ImportError:
        return None
    c = coordinator_from_env()
    return None if c.name == "none" else c


@contextlib.asynccontextmanager
async def keep_registered(coordinator: Any | None) -> AsyncIterator[None]:
    """Keep *coordinator*'s tenant registered with the GPU broker for the body (the server's lifetime), signing off at
    the end, so the broker knows argus-mint before its first bio. Nothing for ``None`` or a local coordinator; never
    blocks or fails startup on the broker's account (argus-cortex's ``gpu.aregistered``)."""
    if coordinator is None:
        yield
        return
    from argus_cortex.gpu import aregistered  # noqa: PLC0415 - optional, in the server extra

    async with aregistered(coordinator):
        yield
