"""Thin HTTP clients for the training steps (``training.py``): argus-curator, argus-lens, argus-forge
and argus-proof. Each sends exactly what the sibling's server takes and returns its JSON as is;
``training`` checks what comes back.

None of the four takes a token: they sit on a private network and refuse cross-site browser
writes by ``Origin``, which a server-to-server call doesn't send. Long calls (a scan, a batch of
captions, a proof run) block until the sibling answers, so their timeouts are long.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

__all__ = ["CuratorClient", "ForgeClient", "LensClient", "ProofClient", "TrainingClientError"]

_SHORT = httpx.Timeout(30.0)
_LONG = httpx.Timeout(30.0, read=6 * 3600.0)  # a scan, a caption batch, a proof run: minutes to hours
_SCORE = httpx.Timeout(30.0, read=600.0)  # one teaser scored: a GPU lease and a model load, with a person waiting


class TrainingClientError(Exception):
    """A sibling answered with an error (``status``), or not at all (``status`` None)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _Http:
    service = "service"

    def __init__(self, base_url: str, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client()

    def _json(self, method: str, path: str, *, timeout: httpx.Timeout = _SHORT, **kw: Any) -> Any:
        try:
            resp = self.client.request(method, f"{self.base_url}{path}", timeout=timeout, **kw)
        except httpx.HTTPError as e:
            raise TrainingClientError(f"{self.service}: {e}") from e
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except (ValueError, AttributeError):
                detail = resp.text[:500]
            raise TrainingClientError(f"{self.service} {method} {path}: {resp.status_code} {detail}", resp.status_code)
        try:
            return resp.json()
        except ValueError as e:
            raise TrainingClientError(f"{self.service} {method} {path}: not JSON", resp.status_code) from e

    def _maybe(self, path: str) -> dict[str, Any] | None:
        try:
            resp = self.client.get(f"{self.base_url}{path}", timeout=_SHORT)
        except httpx.HTTPError as e:
            raise TrainingClientError(f"{self.service}: {e}") from e
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise TrainingClientError(f"{self.service} GET {path}: {resp.status_code} {resp.text[:500]}")
        return resp.json()


def _files(field: str, files: list[tuple[str, bytes]]) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [
        (field, (name, data, "image/png" if name.lower().endswith(".png") else "image/jpeg")) for name, data in files
    ]


class CuratorClient(_Http):
    """argus-curator (:8101). ``POST /upload`` (multipart ``files`` + ``folder``), ``POST /scan/folder``
    (``ScanRequest`` -> ``ScanSummary``, synchronous), ``POST /scan/{id}/select`` (``SelectRequest``
    -> ``SelectResult``), ``POST /export`` (``ExportRequest`` -> ``ExportResult``)."""

    service = "curator"

    def upload(self, folder: str, files: list[tuple[str, bytes]]) -> dict[str, Any]:
        return self._json("POST", "/upload", data={"folder": folder}, files=_files("files", files), timeout=_LONG)

    def scan(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", "/scan/folder", json=body, timeout=_LONG)

    def select(self, scan_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", f"/scan/{scan_id}/select", json=body, timeout=_LONG)

    def export(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", "/export", json=body, timeout=_LONG)


class LensClient(_Http):
    """argus-lens (:8100). ``POST /caption/batch``: multipart ``files`` plus form fields
    (``trigger_word``, ``target_style``, ``target_category``, ``target_backend``…) ->
    ``{results: {name: CaptionResult}, errors: {name: str}}``."""

    service = "lens"

    def caption_batch(self, files: list[tuple[str, bytes]], form: dict[str, str]) -> dict[str, Any]:
        return self._json("POST", "/caption/batch", data=form, files=_files("files", files), timeout=_LONG)


class ForgeClient(_Http):
    """argus-forge (:8103). ``GET /health`` (``training``: enabled|disabled), ``POST /config``
    (``ForgeRequest`` -> ``ForgeResult``), ``POST /run`` (``RunRequest`` -> 202 ``RunState``),
    ``GET /run/{id}`` (``RunState``, with ``lora`` once it succeeded; 404 once a forge without a state
    directory restarts). With training disabled, a ``/config`` that isn't a dry run is a 403."""

    service = "forge"

    def health(self) -> dict[str, Any]:
        return self._json("GET", "/health")

    def config(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", "/config", json=body, timeout=_LONG)

    def run(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", "/run", json=body)

    def status(self, run_id: str) -> dict[str, Any] | None:
        return self._maybe(f"/run/{run_id}")


class ProofClient(_Http):
    """argus-proof (:8104). ``POST /run/stream`` (``RunRequest``; NDJSON frames ending in
    ``complete`` or ``error``), ``GET /report/{run}`` (``EvalReport``), ``GET /report/{run}/manifest``
    (``RunManifest``), ``POST /score`` (multipart ``images`` plus a ``request`` part holding a
    ``ScoreRequest`` as JSON -> ``ScoreResult``; nothing stored)."""

    service = "proof"

    def score(self, images: list[tuple[str, bytes]], request: dict[str, Any]) -> dict[str, Any]:
        """Score ``images`` against an export's references (``identity_check.py``). A 503 is proof
        saying the scorer can't run now (not installed, or the GPU lease was refused)."""
        return self._json(
            "POST", "/score", data={"request": json.dumps(request)}, files=_files("images", images), timeout=_SCORE
        )

    def run(self, body: dict[str, Any]) -> dict[str, Any]:
        last: dict[str, Any] = {"type": "error", "message": "proof's stream ended without a result"}
        try:
            with self.client.stream("POST", f"{self.base_url}/run/stream", json=body, timeout=_LONG) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise TrainingClientError(f"proof POST /run/stream: {resp.status_code} {resp.text[:500]}")
                for line in resp.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        frame = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(frame, dict) and frame.get("type") in ("complete", "error"):
                        last = frame
        except httpx.HTTPError as e:
            raise TrainingClientError(f"proof: {e}") from e
        return last

    def report(self, run_id: str) -> dict[str, Any] | None:
        return self._maybe(f"/report/{run_id}")

    def manifest(self, run_id: str) -> dict[str, Any]:
        return self._json("GET", f"/report/{run_id}/manifest")
