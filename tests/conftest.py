from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def template_doc() -> dict[str, Any]:
    """An approved template as it appears in YAML (before validation)."""
    return {
        "id": "test-portrait",
        "version": 1,
        "status": "approved",
        "approved_by": "reviewer",
        "approved_at": "2026-11-08",
        "model": "juggernaut-xl",
        "loras": ["fantasy-portrait"],
        "width": 1024,
        "height": 1024,
        "prompt": "portrait of a {role} warrior, {colour} heraldry, {pose}",
        "negative": "text, watermark",
        "variables": {
            "role": ["infantry", "ranger"],
            "colour": ["crimson", "none"],
            "pose": ["standing", "on guard"],
        },
        "max_attempts": 4,
    }


@pytest.fixture(scope="session")
def merkle_fixtures() -> dict[str, Any]:
    return json.loads((FIXTURES / "merkle.json").read_text())
