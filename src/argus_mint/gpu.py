"""GPU leases for work argus-mint asks for directly: the bio writer's model.

Renders go through darkroom, which takes its own leases (mint only tells it the priority). A bio is written by a
model in Ollama behind the suite's chat API, so the writer takes a lease on ``ollama:<model>`` first. The coordinator
comes from argus-cortex's environment (``ARGUS_GPU_COORDINATOR``, ``ARGUS_GPU_BROKER_*``, ``ARGUS_GPU_TENANT_MANIFEST``;
see ``gpu-tenant.yaml``), so the argus-iris broker is switched on by configuration alone. Without argus-cortex
installed (a base or ``cli`` install), or with no coordinator configured, nothing is leased.
"""

from __future__ import annotations

from typing import Any

__all__ = ["BIO_KIND", "BIO_MODEL_MB", "BIO_VRAM_MB", "coordinator"]

# The bio kind declared in gpu-tenant.yaml (tests/test_gpu.py keeps them equal). MiB, for mistral-nemo 12B (Q4):
# the broker uses its learned p95 when larger, so a heavier bio model is caught after a few leases.
BIO_KIND = "bio"
BIO_VRAM_MB = 8500
BIO_MODEL_MB = 7100


def coordinator() -> Any | None:
    """The GPU coordinator the environment names, or ``None`` (no argus-cortex, or ``ARGUS_GPU_COORDINATOR=none``)."""
    try:
        from argus_cortex.gpu import coordinator_from_env  # noqa: PLC0415 - optional, in the server extra
    except ImportError:
        return None
    c = coordinator_from_env()
    return None if c.name == "none" else c
