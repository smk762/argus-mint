from __future__ import annotations

import hashlib

import pytest

from argus_mint import merkle


def b(h: str) -> bytes:
    return bytes.fromhex(h.removeprefix("0x"))


def test_fixture_sizes(merkle_fixtures):
    assert [c["n"] for c in merkle_fixtures["cases"]] == [1, 2, 3, 7, 1000]


def test_roots_match_openzeppelin(merkle_fixtures):
    for case in merkle_fixtures["cases"]:
        tree = merkle.build(case["values"])
        assert tree.root == b(case["root"]), f"n={case['n']}"


def test_proofs_match_openzeppelin(merkle_fixtures):
    for case in merkle_fixtures["cases"]:
        tree = merkle.build(case["values"])
        for index, expected in case["proofs"].items():
            proof = tree.proof(int(index))
            assert proof == [b(p) for p in expected], f"n={case['n']} index={index}"
            assert merkle.verify(tree.root, case["values"][int(index)], proof)


def test_dump_matches_openzeppelin(merkle_fixtures):
    for case in merkle_fixtures["cases"]:
        if "dump" in case:
            assert merkle.build(case["values"]).dump() == case["dump"], f"n={case['n']}"


def test_load_round_trips_and_checks(merkle_fixtures):
    case = next(c for c in merkle_fixtures["cases"] if c["n"] == 7)
    tree = merkle.load(case["dump"])
    assert tree.root == b(case["root"])
    assert tree.proof(3) == [b(p) for p in case["proofs"]["3"]]
    tampered = {**case["dump"], "tree": [case["dump"]["tree"][1], *case["dump"]["tree"][1:]]}
    with pytest.raises(ValueError):
        merkle.load(tampered)


def test_verify_rejects_wrong_value_and_proof():
    values = [hashlib.sha256(str(i).encode()).digest() for i in range(5)]
    tree = merkle.build(values)
    proof = tree.proof(2)
    assert merkle.verify(tree.root, values[2], proof)
    assert not merkle.verify(tree.root, values[3], proof)
    assert not merkle.verify(tree.root, values[2], proof[:-1])
    assert not merkle.verify(b"\x00" * 32, values[2], proof)


def test_single_leaf_tree():
    v = b"\x11" * 32
    tree = merkle.build([v])
    assert tree.root == merkle.leaf(v)
    assert tree.proof(0) == []
    assert merkle.verify(tree.root, v, [])


def test_errors():
    with pytest.raises(ValueError):
        merkle.build([])
    with pytest.raises(ValueError):
        merkle.build([b"short"])
    with pytest.raises(IndexError):
        merkle.build([b"\x00" * 32]).proof(1)


def test_accepts_hex_and_bytes_alike():
    vals = [hashlib.sha256(bytes([i])).digest() for i in range(4)]
    assert merkle.build(vals).root == merkle.build(["0x" + v.hex() for v in vals]).root
