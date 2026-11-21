"""Identity references: cortex provenance, cleared kinds, and a real person's consent record."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

pytest.importorskip("argus_cortex")

from argus_mint.projects import Identity  # noqa: E402
from argus_mint.references import ConsentStore, reference_problems  # noqa: E402

IMAGE = "aa" * 32
RELEASE = "bb" * 32
SUBJECT = "Jo Example"
AT = datetime(2026, 11, 18, 12, tzinfo=UTC)
CONSENTED = {
    "kind": "consented",
    "source": "shoot",
    "license": "release",
    "subject": SUBJECT,
    "release_sha256": RELEASE,
}


def _identity(*provenances: dict) -> Identity:
    refs = [{"file": f"r{i}.png", "sha256": IMAGE, "provenance": p} for i, p in enumerate(provenances)]
    return Identity(
        id="keeper", version=1, status="draft", project="p", voice="keeper", presence="embodied",
        prompt="an old keeper", references=refs,
    )  # fmt: skip


def _record(**over) -> dict:
    return {
        "release_sha256": RELEASE,
        "subject": SUBJECT,
        "collected_by": "studio",
        "signed_on": "2026-01-01",
        "expires_on": None,
        "age_check": {"checked_by": "studio", "checked_on": "2026-01-01"},
        "face": True,
        "voice": False,
        "uses": ["training"],
        "reference_images": [IMAGE],
        "revocable": True,
        "notice_days": 30,
        "on_revoke": "delete",
    } | over


@pytest.fixture
def store(tmp_path: Path):
    def write(**over) -> ConsentStore:
        (tmp_path / f"{RELEASE}.json").write_text(json.dumps(_record(**over)))
        return ConsentStore(tmp_path)

    return write


def test_cleared_sources_pass():
    synthetic = {"kind": "synthetic", "source": "mint render", "license": "own",
                 "generator": {"model": "sdxl/x.safetensors", "seed": 7, "prompt": "an old keeper"}}  # fmt: skip
    stock = {"kind": "stock", "source": "a library", "license": "CC0"}
    assert reference_problems(_identity(synthetic, stock), None, at=AT) == []


def test_uncleared_and_invalid_provenance_are_refused():
    found = reference_problems(_identity({"kind": "personal", "source": "my phone"}, {"kind": "stock"}), None, at=AT)
    assert "a personal source isn't cleared" in found[0]
    assert "isn't a valid argus-cortex Provenance" in found[1]


def test_a_consented_reference_needs_a_valid_record_that_binds_it(store):
    ident = _identity(CONSENTED)
    assert "no consent records are configured" in reference_problems(ident, None, at=AT)[0]
    assert reference_problems(ident, store(), at=AT) == []
    assert "doesn't bind this image" in reference_problems(ident, store(reference_images=["cc" * 32]), at=AT)[0]
    assert "another release or subject" in reference_problems(ident, store(subject="Someone Else"), at=AT)[0]
    assert "(use-not-covered)" in reference_problems(ident, store(uses=["avatar"]), at=AT)[0]
    assert "(expired)" in reference_problems(ident, store(expires_on="2026-06-01"), at=AT)[0]
    revoked = {"requested_on": "2026-10-01", "effective_on": "2026-11-01", "by": "subject"}
    assert "(revoked)" in reference_problems(ident, store(revocation=revoked), at=AT)[0]


def test_a_missing_or_broken_record_is_a_problem_that_names_no_one(store, tmp_path):
    ident = _identity(CONSENTED)
    assert "no consent record" in reference_problems(ident, ConsentStore(tmp_path / "empty"), at=AT)[0]
    store(age_check={"checked_by": "studio", "checked_on": "2026-02-01"})  # checked after signing: invalid
    found = reference_problems(ident, ConsentStore(tmp_path), at=AT)
    assert "consent record is invalid" in found[0]
    assert all(SUBJECT not in p and "Jo" not in p for p in found)
    (tmp_path / f"{RELEASE}.json").write_bytes(b'{"subject": "\xff"}')  # not UTF-8: a reason, not a crash
    assert "isn't UTF-8" in reference_problems(ident, ConsentStore(tmp_path), at=AT)[0]


def test_a_bad_release_hash_never_becomes_a_path(tmp_path):
    ident = _identity(CONSENTED | {"release_sha256": "../../etc/passwd"})
    assert "isn't a valid argus-cortex Provenance" in reference_problems(ident, ConsentStore(tmp_path), at=AT)[0]
    assert ConsentStore(tmp_path).get("../x") == "not a release sha256"


def test_consent_is_read_fresh_so_a_revocation_counts_at_once(store):
    s = store()
    ident = _identity(CONSENTED)
    assert reference_problems(ident, s, at=AT) == []
    store(revocation={"requested_on": "2026-11-17", "effective_on": "2026-11-18", "by": "subject"})
    assert "(revoked)" in reference_problems(ident, s, at=AT)[0]
    assert date(2026, 11, 18) == AT.date()
