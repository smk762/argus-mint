"""GPU leases for the bio writer, through argus-cortex's coordinator (the argus-iris broker, #33)."""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

import httpx
import pytest
from argus_cortex.gpu import GpuLease, GpuLeaseRefused, GpuLeaseTimeout
from argus_cortex.gpu_broker import BrokerCoordinator
from argus_cortex.gpu_wire import load_tenant_manifest
from fastapi.testclient import TestClient

from argus_mint import gpu
from argus_mint.bios import BioError, ChatWriter
from argus_mint.server import Settings, create_app

MANIFEST = Path(__file__).parent.parent / "gpu-tenant.yaml"
REPLY = {"choices": [{"message": {"content": "{name} sings."}}]}


class Coordinator:
    name = "fake"

    def __init__(self, fail: Exception | None = None):
        self.leases: list[dict] = []
        self.held = False
        self.fail = fail

    @contextlib.contextmanager
    def lease(self, **kw):
        if self.fail:
            raise self.fail
        self.leases.append(kw)
        self.held = True
        try:
            yield GpuLease(via="broker")
        finally:
            self.held = False


def writer(coordinator, seen=None, **kw) -> ChatWriter:
    def chat(request):
        if seen is not None:
            seen.append(coordinator.held)
        return httpx.Response(200, json=REPLY)

    return ChatWriter(
        "http://writer", client=httpx.Client(transport=httpx.MockTransport(chat)), coordinator=coordinator, **kw
    )


def test_the_manifest_validates_and_matches_the_code():
    m = load_tenant_manifest(MANIFEST)
    assert m.tenant == "argus-mint" and m.residents == ()
    bio = m.kind_for(gpu.BIO_KIND)
    assert bio is not None and bio.target == "ollama:mistral-nemo" and bio.priority == "interactive"
    assert (bio.vram_mb, bio.model_mb) == (gpu.BIO_VRAM_MB, gpu.BIO_MODEL_MB)
    assert [k.kind for k in m.kinds] == [gpu.BIO_KIND]  # mint leases nothing else: darkroom leases renders


def test_a_bio_is_written_under_a_lease_on_its_ollama_model():
    c, seen = Coordinator(), []
    w = writer(c, seen)
    assert w.write(model="mistral-nemo", system="s", prompt="p", temperature=0.5, max_tokens=10) == "{name} sings."
    assert seen == [True]  # the request went out while the lease was held
    assert c.leases == [
        {
            "caller": "argus-mint:bio", "min_vram_mb": gpu.BIO_VRAM_MB, "model_mb": gpu.BIO_MODEL_MB,
            "target": "ollama:mistral-nemo", "kind": "bio", "priority": None,
        }
    ]  # fmt: skip
    w.batch().write(model="qwen3:8b", system="s", prompt="p", temperature=0.5, max_tokens=10)
    assert c.leases[1]["priority"] == "batch" and c.leases[1]["target"] == "ollama:qwen3:8b"
    assert w.priority is None  # batch() is a copy


@pytest.mark.parametrize("fail", [GpuLeaseTimeout("waited 600 s"), GpuLeaseRefused("too big")])
def test_no_lease_is_a_writer_failure(fail):
    with pytest.raises(BioError, match="no GPU lease"):
        writer(Coordinator(fail)).write(model="m", system="s", prompt="p", temperature=0.5, max_tokens=10)


def test_no_coordinator_leases_nothing():
    assert writer(None).write(model="m", system="s", prompt="p", temperature=0.5, max_tokens=10) == "{name} sings."


def test_the_coordinator_comes_from_the_environment(monkeypatch):
    for k in ("ARGUS_GPU_COORDINATOR", "ARGUS_GPU_BROKER_URL", "ARGUS_GPU_TENANT", "ARGUS_GPU_TENANT_MANIFEST"):
        monkeypatch.delenv(k, raising=False)
    assert gpu.coordinator() is None  # unset: no lease
    # The URL alone is the broker, the tenant read from ./gpu-tenant.yaml (the image's is /app/gpu-tenant.yaml).
    monkeypatch.chdir(MANIFEST.parent)
    monkeypatch.setenv("ARGUS_GPU_BROKER_URL", "http://broker:8120")
    monkeypatch.setenv("ARGUS_GPU_BROKER_FALLBACK", "none")
    c = gpu.coordinator()
    assert isinstance(c, BrokerCoordinator) and c.tenant == "argus-mint"
    assert c.manifest == load_tenant_manifest(MANIFEST)
    assert c.fallback.name == "none"
    monkeypatch.setenv("ARGUS_GPU_COORDINATOR", "none")  # still opts out with the URL set
    assert gpu.coordinator() is None


class FakeBroker:
    """The broker's tenant routes, recording what reached them."""

    def __init__(self, down: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.manifests: list[dict] = []
        self.down = down

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        self.calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/tenants":
            self.manifests.append(json.loads(request.content))
            return httpx.Response(200, json={"tenant": "argus-mint"})
        if request.method == "DELETE" and request.url.path == "/tenants/argus-mint":
            return httpx.Response(204)
        return httpx.Response(404)


def _app(tmp_path, coordinator):
    w = ChatWriter("http://writer", coordinator=coordinator) if coordinator is not None else None
    return create_app(Settings(drafts_dir=tmp_path / "drafts", writer_url="http://writer"), writer=w)


def _broker_coordinator(broker: FakeBroker) -> BrokerCoordinator:
    return BrokerCoordinator(
        "http://broker:8120",
        tenant="argus-mint",
        manifest=load_tenant_manifest(MANIFEST),
        async_transport=httpx.MockTransport(broker),
        detect_container=False,
    )


def test_the_server_registers_the_writers_tenant_at_startup_and_signs_off_at_shutdown(tmp_path):
    broker = FakeBroker()
    with TestClient(_app(tmp_path, _broker_coordinator(broker))) as client:
        deadline = time.monotonic() + 5
        while ("POST", "/tenants") not in broker.calls:
            assert time.monotonic() < deadline, "never registered"
            time.sleep(0.01)
        assert client.get("/health").status_code == 200
    assert broker.calls[-1] == ("DELETE", "/tenants/argus-mint")
    assert broker.manifests[0]["tenant"] == "argus-mint"
    assert [k["kind"] for k in broker.manifests[0]["kinds"]] == [gpu.BIO_KIND]


def test_a_down_broker_or_no_coordinator_never_stops_the_server_starting(tmp_path):
    with TestClient(_app(tmp_path, _broker_coordinator(FakeBroker(down=True)))) as client:
        assert client.get("/health").status_code == 200
    with TestClient(_app(tmp_path, None)) as client:
        assert client.get("/health").status_code == 200
