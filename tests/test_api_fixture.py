"""The recorded API fixture (fixtures/api) that argus-studio's and argus-assay's demo mode serve.

Regenerate with ``uv run python scripts/api_fixture.py`` when a test here fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from argus_mint.wire import WIRE_VERSION, BioInfo, DraftDetail, FrameInfo, Health, LibraryListing, TemplateInfo

API = Path(__file__).resolve().parents[1] / "fixtures" / "api"


def load(rel: str):
    return json.loads((API / rel).read_text(encoding="utf-8"))


def test_the_fixture_is_recorded_at_the_current_wire_version():
    health = Health.model_validate(load("health.json"))
    assert health.wire_version == WIRE_VERSION, "fixtures/api is stale: run scripts/api_fixture.py"
    assert health.capabilities.library


@pytest.mark.parametrize(
    ("rel", "model"),
    [("templates.json", list[TemplateInfo]), ("frames.json", list[FrameInfo]), ("bios.json", list[BioInfo])],
)
def test_listings_parse_as_wire_models(rel, model):
    assert TypeAdapter(model).validate_python(load(rel))


def test_every_listed_draft_is_recorded():
    details = [DraftDetail.model_validate(load(f"drafts/{i['id']}.json")) for i in load("drafts.json")["items"]]
    for d in details:
        for which in ("image", "art"):
            assert (API / "drafts" / d.id / f"{which}.png").is_file()
    assert {d.status for d in details} >= {"pending", "approved", "rejected", "published"}
    assert any(d.draft.library for d in details), "one draft should come from the library"


def test_library_groups_are_whole_and_every_item_has_its_image():
    listing = LibraryListing.model_validate(load("library.json"))
    groups: dict[int, set[str]] = {}
    for item in listing.items:
        groups.setdefault(item.group, set()).add(item.model)
        assert (API / "library" / f"{item.id}.jpg").is_file(), f"{item.id} has no image"
    assert listing.count == len(groups)
    assert all(models == set(listing.models) for models in groups.values()), "a group misses a checkpoint"
    extra = {p.stem for p in (API / "library").glob("*.jpg")} - {i.id for i in listing.items}
    assert not extra, f"images for unlisted entries: {sorted(extra)}"
