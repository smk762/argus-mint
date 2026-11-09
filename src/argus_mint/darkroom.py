"""The real :class:`~argus_mint.render.Darklight` client: argus-darkroom's guarded generation API.

darkroom pre-flights the prompt, renders through ComfyUI, scans the image under the
enforcing policy and releases it only when it passes. This client maps its outcomes onto
argus-mint's render protocol:

| darkroom outcome | render outcome | why |
|---|---|---|
| ``passed`` | ``ok`` | the image is fetched and its sha256 checked against the record |
| ``review`` | ``safety_rejected`` (``ok`` with ``accept_review=True``) | a flagged card is not minted by default |
| ``deleted``, ``omitted`` | ``safety_rejected`` | retried with the next seed (hard rule 3) |
| ``refused`` | ``error`` | pre-flight judged the prompt itself; another seed can't change that, the template needs review |
| ``failed`` | ``error`` | generation broke; not a safety verdict |

Requests never use darkroom's Ollama expansion (that would replace the template's prompt,
breaking hard rules 1 and 7), always name the enforcing policy (default ``default``, the
SFW policy), and the returned record is checked to be the render that was asked for.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import structlog

from argus_mint.render import GenResult
from argus_mint.templates import RenderRequest

__all__ = ["DarkroomClient", "DarkroomError", "final_prompt", "request_body"]

log = structlog.get_logger(__name__)


class DarkroomError(Exception):
    """darkroom could not be asked (bad request, unreachable, wrong answer)."""


def final_prompt(req: RenderRequest) -> str:
    """The prompt darkroom will send to ComfyUI: the template prompt, then trigger words not
    already present, comma-separated (darkroom's ``assemble_prompt``)."""
    body = req.prompt.strip()
    present = body.casefold()
    extra: list[str] = []
    for lora in req.loras:
        for word in lora.trigger_words:
            w = word.strip()
            if w and w.casefold() not in present and w not in extra:
                extra.append(w)
    return ", ".join(part for part in (body, *extra) if part)


def request_body(
    req: RenderRequest,
    *,
    policy: str = "default",
    advisory_policies: tuple[str, ...] = (),
    thinking: str | None = None,
    for_preflight: bool = False,
) -> dict[str, Any]:
    """The ``POST /generate/stream`` body for a filled template (``for_preflight``: the
    ``/preflight`` body, where sampling is optional because nothing is rendered)."""
    if req.sampling is None and not for_preflight:
        raise DarkroomError(
            f"template {req.template_id} v{req.template_version} has no sampling block; darkroom needs one"
        )
    body: dict[str, Any] = {
        "user_prompt": req.prompt,
        "negative_prompt": req.negative,
        "expand": False,
        "seed": req.seed,
        "checkpoint": req.model,
        "sampling": {**req.sampling.model_dump(), "width": req.width, "height": req.height} if req.sampling else None,
        "loras": [{"name": lo.name, "weight": lo.weight, "trigger_words": list(lo.trigger_words)} for lo in req.loras],
        "enforcing_policy": policy,
        "advisory_policies": list(advisory_policies),
    }
    if thinking is not None:
        body["thinking"] = thinking
    return body


def _sse_events(lines: Iterator[str]) -> Iterator[tuple[str, Any]]:
    event, data = "message", []
    for line in lines:
        if line == "":
            if data:
                yield event, json.loads("\n".join(data))
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, json.loads("\n".join(data))


class DarkroomClient:
    """Renders through argus-darkroom. Implements :class:`argus_mint.render.Darklight`."""

    def __init__(
        self,
        base_url: str = "http://localhost:8106",
        *,
        policy: str = "default",
        advisory_policies: tuple[str, ...] = (),
        thinking: str | None = None,
        accept_review: bool = False,
        busy_wait: float = 5.0,
        busy_timeout: float = 600.0,
        client: httpx.Client | None = None,
        timeout: float = 900.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.policy = policy
        self.advisory_policies = advisory_policies
        self.thinking = thinking
        self.accept_review = accept_review
        self.busy_wait = busy_wait
        self.busy_timeout = busy_timeout
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))

    def _body(self, req: RenderRequest, *, for_preflight: bool = False) -> dict[str, Any]:
        return request_body(
            req,
            policy=self.policy,
            advisory_policies=self.advisory_policies,
            thinking=self.thinking,
            for_preflight=for_preflight,
        )

    # -- checks ---------------------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        resp = self.client.get(f"{self.base_url}/health")
        resp.raise_for_status()
        return resp.json()

    def preflight(self, req: RenderRequest) -> dict[str, Any]:
        """darkroom's text check alone (no render). ``outcome`` is ``"ok"`` or a refusal."""
        resp = self.client.post(f"{self.base_url}/preflight", json=self._body(req, for_preflight=True))
        if resp.status_code == 400:
            raise DarkroomError(f"darkroom refused the request: {resp.text}")
        resp.raise_for_status()
        return resp.json()

    def check_text(self, text: str) -> tuple[bool, str]:
        """Screen card text (a bio) with darkroom's text pre-flight under this client's policy.

        Implements :class:`argus_mint.bios.TextGuard`: ``(passes, detail)``.
        """
        body = {
            "user_prompt": text,
            "negative_prompt": "",
            "expand": False,
            "enforcing_policy": self.policy,
            "advisory_policies": list(self.advisory_policies),
        }
        try:
            resp = self.client.post(f"{self.base_url}/preflight", json=body)
            if resp.status_code == 400:
                raise DarkroomError(f"darkroom refused the request: {resp.text}")
            resp.raise_for_status()
            verdict = resp.json()
        except httpx.HTTPError as e:
            raise DarkroomError(f"darkroom text check failed: {e}") from e
        if verdict.get("outcome") == "ok":
            return True, ""
        refusal = verdict.get("refusal") or {}
        return False, (refusal.get("rule") or refusal.get("reason") or verdict.get("outcome") or "refused")

    # -- generation -----------------------------------------------------------------------------

    def _stream(self, req: RenderRequest) -> dict[str, Any] | str:
        """Run one generation; return the record, or the error detail. Waits while darkroom is busy."""
        deadline = time.monotonic() + self.busy_timeout
        while True:
            with self.client.stream("POST", f"{self.base_url}/generate/stream", json=self._body(req)) as resp:
                if resp.status_code == 409 and time.monotonic() < deadline:
                    log.info("darkroom_busy", wait=self.busy_wait)
                else:
                    if resp.status_code == 400:
                        resp.read()
                        raise DarkroomError(f"darkroom refused the request: {resp.text}")
                    resp.raise_for_status()
                    for event, data in _sse_events(resp.iter_lines()):
                        if event == "complete":
                            return data["record"]
                        if event == "error":
                            return str(data.get("detail", "unknown error"))
                    return "the stream ended without a record"
            time.sleep(self.busy_wait)

    def _check_record(self, record: dict[str, Any], req: RenderRequest) -> str | None:
        r = record.get("request") or {}
        if r.get("expanded_prompt") is not None:
            return "darkroom expanded the prompt"
        if r.get("final_prompt") != final_prompt(req):
            return "darkroom rendered a different prompt"
        if r.get("seed") != req.seed:
            return f"darkroom used seed {r.get('seed')}, not {req.seed}"
        if (record.get("guard") or {}).get("enforcing_policy", self.policy) != self.policy:
            return "darkroom enforced a different policy"
        return None

    def generate(self, req: RenderRequest) -> GenResult:
        try:
            outcome = self._stream(req)
        except httpx.HTTPError as e:
            return GenResult.error(f"darkroom unreachable or failed: {e}")
        if isinstance(outcome, str):
            return GenResult.error(outcome)
        record = outcome
        rid, verdict = str(record.get("id", "")), record.get("outcome")
        reason = record.get("outcome_reason") or ""
        log.info("darkroom_record", id=rid, outcome=verdict, reason=reason, seed=req.seed)

        if verdict in ("deleted", "omitted") or (verdict == "review" and not self.accept_review):
            return GenResult.safety_rejected(f"{verdict}: {reason}".rstrip(": "), ref=rid)
        if verdict == "refused":
            return GenResult.error(f"pre-flight refused the template prompt ({reason}); the template needs review", rid)
        if verdict not in ("passed", "review"):
            return GenResult.error(f"{verdict}: {reason}".rstrip(": "), ref=rid)
        if problem := self._check_record(record, req):
            return GenResult.error(problem, ref=rid)
        try:
            img = self.client.get(f"{self.base_url}/image/{rid}")
            img.raise_for_status()
        except httpx.HTTPError as e:
            return GenResult.error(f"could not fetch image {rid}: {e}", ref=rid)
        expected = (record.get("image") or {}).get("sha256")
        if not expected or hashlib.sha256(img.content).hexdigest() != expected:
            return GenResult.error(f"image {rid} does not match the record's sha256", ref=rid)
        return GenResult.ok(img.content, ref=rid)
