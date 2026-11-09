from __future__ import annotations

import json

import pytest

from argus_mint.metadata import Attribute, Provenance, canonical, card_metadata, content_hash, ipfs_uri

CID = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
PROV = Provenance(
    template="test-portrait",
    template_version=1,
    template_hash="0x" + "ab" * 32,
    seed=81234,
    attempts=2,
    asset_id="0x" + "01" * 32,
    consumer="atlas-realms",
)


def make(**overrides):
    kw = dict(
        name="Hana of the Strait",
        description="Ranger of clan Kāhu.",
        image_cid=CID,
        external_url="https://example.org/cards/17",
        attributes=[
            Attribute(trait_type="Class", value="ranger"),
            Attribute(trait_type="Kills", value=12, display_type="number"),
        ],
        provenance=PROV,
    )
    kw.update(overrides)
    return card_metadata(**kw)


def test_card_metadata_shape():
    doc = make()
    assert doc["image"] == f"ipfs://{CID}"
    assert doc["attributes"][0] == {"trait_type": "Class", "value": "ranger"}
    assert doc["attributes"][1] == {"trait_type": "Kills", "display_type": "number", "value": 12}
    assert doc["provenance"]["template_hash"] == "0x" + "ab" * 32
    assert doc["provenance"]["asset_id"] == "0x" + "01" * 32


def test_canonical_is_byte_stable():
    a = canonical(make())
    b = canonical(json.loads(a))
    assert a == b
    assert b"\n" not in a and b": " not in a
    assert "Kāhu".encode() in a  # UTF-8, not \u escapes
    keys = list(json.loads(a).keys())
    assert keys == sorted(keys)


def test_canonical_known_bytes():
    assert canonical({"b": 1, "a": [3, {"d": "é", "c": None}]}) == '{"a":[3,{"c":null,"d":"é"}],"b":1}'.encode()


def test_canonical_rejects_nan():
    with pytest.raises(ValueError):
        canonical({"x": float("nan")})


def test_content_hash_ignores_live_fields():
    live = make()
    refreshed = make(
        attributes=[Attribute(trait_type="Kills", value=40, display_type="number")],
        description="Fell at Wellington.",
    )
    assert content_hash(live) == content_hash(refreshed)
    assert content_hash(live) != content_hash(make(provenance=PROV.model_copy(update={"seed": 1})))
    assert content_hash(live) != content_hash(make(image_cid="Qm" + "a" * 44))


def test_content_hash_known_value():
    from argus_mint._codec import keccak256

    doc = make()
    expected = keccak256(canonical({"image": doc["image"], "provenance": doc["provenance"]}))
    assert content_hash(doc) == expected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trait_type": "Kills", "value": "twelve", "display_type": "number"},
        {"trait_type": "Brave", "value": True},
        {"trait_type": "X", "value": float("inf")},
        {"trait_type": "", "value": 1},
    ],
)
def test_attribute_type_checks(kwargs):
    with pytest.raises(ValueError):
        Attribute(**kwargs)


def test_duplicate_trait_rejected():
    with pytest.raises(ValueError, match="twice"):
        make(attributes=[Attribute(trait_type="A", value=1), Attribute(trait_type="A", value=2)])


@pytest.mark.parametrize("cid", ["", f"ipfs://{CID}", "not-a-cid", "Qm123"])
def test_bad_cid_rejected(cid):
    with pytest.raises(ValueError):
        ipfs_uri(cid)


def test_external_url_optional_and_checked():
    assert "external_url" not in make(external_url=None)
    with pytest.raises(ValueError):
        make(external_url="javascript:alert(1)")


def test_provenance_validation():
    with pytest.raises(ValueError):
        PROV.model_validate({**PROV.model_dump(), "asset_id": "0x1234"})
    with pytest.raises(ValueError):
        PROV.model_validate({**PROV.model_dump(), "attempts": 0})


def test_unset_render_choices_leave_the_content_hash_as_it_was():
    # A provenance without variables, model or LoRAs (cards minted before they were recorded)
    # serializes, and so hashes, exactly as it did.
    assert set(PROV.to_json()) == {
        "template",
        "template_version",
        "template_hash",
        "seed",
        "attempts",
        "asset_id",
        "consumer",
    }
    with_choices = PROV.model_copy(update={"variables": {"role": "ranger"}, "model": "sdxl/a/a.safetensors"})
    assert content_hash(make(provenance=with_choices)) != content_hash(make())


def test_whole_style_numbers_hash_as_a_js_verifier_hashes_them():
    # JavaScript writes 1 where Python writes 1.0, so a whole float in provenance must be written
    # as an int, or a JS verifier can't reproduce the contentHash.
    from argus_mint.metadata import CardStyle

    style = CardStyle.model_validate(
        {
            "tint": {"colour": "#112233", "strength": 1},
            "slots": {"bio": {"background": {"colour": "#000000", "opacity": 0}}},
        }
    )
    doc = make(provenance=PROV.model_copy(update={"style": style}))
    js_view = json.loads(json.dumps(doc), parse_float=lambda s: int(float(s)) if float(s).is_integer() else float(s))
    assert canonical(js_view) == canonical(doc)
    assert b'"strength":1,' in canonical(doc) or b'"strength":1}' in canonical(doc)
    assert b'"opacity":0,' in canonical(doc)
