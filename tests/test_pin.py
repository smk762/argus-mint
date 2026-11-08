from __future__ import annotations

import json

import httpx
import pytest

from argus_mint._codec import canonical
from argus_mint.metadata import ipfs_uri
from argus_mint.pin import KuboPinner, LocalPinner, PinError, raw_cid


def test_raw_cid_known_value():
    # `ipfs add --cid-version=1 --only-hash` of the 12 bytes "hello world\n"
    assert raw_cid(b"hello world\n") == "bafkreifjjcie6lypi6ny7amxnfftagclbuxndqonfipmb64f2km2devei4"


def test_local_pinner_is_deterministic():
    p = LocalPinner()
    a = p.pin_bytes(b"img", "a.png")
    assert a == LocalPinner().pin_bytes(b"img", "other-name.png")
    assert p.store[a] == b"img"
    doc = {"b": 1, "a": 2}
    assert p.pin_json(doc, "m.json") == raw_cid(canonical(doc))
    ipfs_uri(a)  # a valid CID for metadata


def test_kubo_pinner_posts_and_parses():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"Name": "a.png", "Hash": "bafyfake", "Size": "3"})

    p = KuboPinner("http://ipfs:5001/", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert p.pin_bytes(b"img", "a.png") == "bafyfake"
    assert seen["url"].startswith("http://ipfs:5001/api/v0/add?")
    assert "cid-version=1" in seen["url"] and "pin=true" in seen["url"]
    assert b"img" in seen["body"] and b'filename="a.png"' in seen["body"]


def test_kubo_pinner_pins_canonical_json():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, text=json.dumps({"Hash": "bafyjson"}))

    p = KuboPinner(client=httpx.Client(transport=httpx.MockTransport(handler)))
    p.pin_json({"z": 1, "a": "é"}, "m.json")
    assert canonical({"z": 1, "a": "é"}) in bodies[0]


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500, text="boom"), httpx.Response(200, json={"nope": 1}), httpx.Response(200, text="not json")],
)
def test_kubo_pinner_errors(response):
    p = KuboPinner(client=httpx.Client(transport=httpx.MockTransport(lambda r: response)))
    with pytest.raises(PinError):
        p.pin_bytes(b"x", "x")
