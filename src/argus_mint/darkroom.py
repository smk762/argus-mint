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

import base64
import copy
import hashlib
import json
import re
import time
from collections.abc import Iterator
from typing import Any

import httpx
import structlog

from argus_mint.render import GenResult, InitImage
from argus_mint.templates import RenderRequest, Template, family

__all__ = [
    "DarkroomClient",
    "DarkroomError",
    "check_inpaint",
    "check_render",
    "check_template",
    "final_prompt",
    "lora_counts",
    "request_body",
]

log = structlog.get_logger(__name__)

_LIST_TIMEOUT = 30.0  # seconds, for GET /models


class DarkroomError(Exception):
    """darkroom could not be asked (bad request, unreachable, wrong answer)."""


def final_prompt(req: RenderRequest) -> str:
    """The prompt darkroom will send to ComfyUI: the template prompt, then trigger words not
    already present, comma-separated (darkroom's ``assemble_prompt``, from 0.2.0).

    Present means as a whole word or phrase, in any case, in the prompt or the trigger words
    already added: ``sks`` inside ``tasks`` is not the trigger, and ``Foo`` after ``foo`` is the same one.
    """
    body = req.prompt.strip()
    extra: list[str] = []
    for lora in req.loras:
        for word in lora.trigger_words:
            w = word.strip()
            if w and not _has_phrase(", ".join((body, *extra)), w):
                extra.append(w)
    return ", ".join(part for part in (body, *extra) if part)


def _has_phrase(text: str, phrase: str) -> bool:
    """Whether ``phrase`` is in ``text`` with no word character either side of it, ignoring case."""
    return re.search(rf"(?<!\w){re.escape(phrase.casefold())}(?!\w)", text.casefold()) is not None


def request_body(
    req: RenderRequest,
    *,
    policy: str = "default",
    advisory_policies: tuple[str, ...] = (),
    thinking: str | None = None,
    for_preflight: bool = False,
    init: InitImage | None = None,
) -> dict[str, Any]:
    """The ``POST /generate/stream`` body for a filled template (``for_preflight``: the
    ``/preflight`` body, where sampling is optional because nothing is rendered).

    ``init`` is the input image and mask of an inpainting request (darkroom's ``init_image``):
    it must be the one ``req.init_image`` records, and a request that records one needs it.
    """
    if (init is None) != (req.init_image is None) or (init is not None and init.ref() != req.init_image):
        raise DarkroomError("the input image sent must be the one the request records")
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
        "sampling": {**req.sampling.model_dump(exclude_none=True), "width": req.width, "height": req.height}
        if req.sampling
        else None,
        "loras": [{"name": lo.name, "weight": lo.weight, "trigger_words": list(lo.trigger_words)} for lo in req.loras],
        "enforcing_policy": policy,
        "advisory_policies": list(advisory_policies),
    }
    if thinking is not None:
        body["thinking"] = thinking
    if init is not None:
        body["init_image"] = {
            "data_base64": base64.b64encode(init.image).decode("ascii"),
            "denoise": init.denoise,
            **({"mask_base64": base64.b64encode(init.mask).decode("ascii")} if init.mask is not None else {}),
        }
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


def lora_counts(t: Template, model: str) -> list[int]:
    """How many LoRAs a render of ``t`` on ``model`` can carry: its own, plus 0..max_loras chosen."""
    fixed = len(t.loras)
    choosable = [o for o in t.lora_options if not o.models or model in o.models]
    return list(range(fixed, fixed + min(t.max_loras, len(choosable)) + 1))


def check_template(t: Template, models: dict[str, Any]) -> list[str]:
    """What would make darkroom refuse a render of ``t``, judged from its ``GET /models``.

    For every checkpoint the template allows: darkroom has it, in the family the template
    thinks; a workflow renders it with every LoRA count the template can ask for; and each such
    workflow has a slot for what the template sends (a negative prompt, ``guidance``, a
    ``clip_skip`` above 2) and gets ``guidance`` when it has a slot for it. Also: every LoRA, the
    sampler and the scheduler exist. Pure; an empty list means nothing was found.
    """
    problems: list[str] = []
    checkpoints = {c["name"]: c for c in models.get("checkpoints") or [] if isinstance(c, dict) and "name" in c}
    # A list darkroom sends is authoritative even when empty (no LoRA files means none render);
    # only a list it doesn't send at all goes unchecked.
    loras = (
        {x if isinstance(x, str) else x.get("name") for x in models["loras"] or [] if isinstance(x, str | dict)}
        if "loras" in models
        else None
    )
    # darkroom picks a workflow of the checkpoint's own family before a family-agnostic one.
    workflows = sorted(
        (w for w in models.get("workflows") or [] if isinstance(w, dict)), key=lambda w: w.get("family") is None
    )
    samplers, schedulers = set(models.get("samplers") or []), set(models.get("schedulers") or [])
    for lora in [*(x.name for x in t.loras), *(o.name for o in t.lora_options)]:
        if loras is not None and lora not in loras:
            problems.append(f"LoRA {lora}: darkroom has no such file")
    for model in t.allowed_models():
        c = checkpoints.get(model)
        if c is None:
            problems.append(f"{model}: darkroom has no such checkpoint")
            continue
        if c.get("family") is not None and c["family"] != family(model):
            problems.append(f"{model}: darkroom puts it in family {c['family']!r}, the template in {family(model)!r}")
        fam = c.get("family") or family(model)  # darkroom reads the family from the path when it doesn't say
        s = t.sampling_for(model)
        if s is None:
            problems.append(f"{model}: the template has no sampling for it (darkroom requires one)")
            continue
        if samplers and s.sampler not in samplers:
            problems.append(f"{model}: darkroom has no sampler {s.sampler!r}")
        if schedulers and s.scheduler not in schedulers:
            problems.append(f"{model}: darkroom has no scheduler {s.scheduler!r}")
        offered = set(c.get("lora_counts") or [])
        wanted = lora_counts(t, model)
        if not offered:
            problems.append(f"{model}: no darkroom workflow renders it")
            continue
        missing = [n for n in wanted if n not in offered]
        if missing:
            problems.append(
                f"{model}: no darkroom workflow renders it with {missing} LoRA(s) (it has {sorted(offered)})"
            )
        for n in (n for n in wanted if n in offered):
            wf = next((w for w in workflows if w.get("family") in (None, fam) and w.get("loras") == n), None)
            if wf is None:
                continue
            slots, at = set(wf.get("slots") or []), f"{model} with {n} LoRA(s) (workflow {wf.get('name')})"
            if t.negative_for(model).strip() and "$negative" not in slots:
                problems.append(f'{at}: takes no negative prompt; set negative_by_family[{family(model)!r}] to ""')
            if s.guidance is not None and "$guidance" not in slots:
                problems.append(f"{at}: takes no guidance, but the template sets it")
            if s.guidance is None and "$guidance" in slots:
                problems.append(f"{at}: needs guidance, but the template's sampling sets none")
            if s.clip_skip > 2 and "$clip_skip" not in slots:
                problems.append(f"{at}: takes no clip_skip, but the template sets {s.clip_skip}")
    return list(dict.fromkeys(problems))


def check_render(models: dict[str, Any], model: str, loras: int) -> list[str]:
    """What would make darkroom refuse a text-to-image render on ``model`` with ``loras`` LoRAs, from its
    ``GET /models`` (``lora_counts``: it picks ``<family>.json`` or ``<family>+lora<N>.json``). Pure."""
    c = next(
        (c for c in models.get("checkpoints") or [] if isinstance(c, dict) and c.get("name") == model),
        None,
    )
    if c is None:
        return [f"{model}: darkroom has no such checkpoint"]
    offered = sorted(n for n in c.get("lora_counts") or [] if isinstance(n, int))
    if loras not in offered:
        return [f"{model}: no darkroom workflow renders it with {loras} LoRA(s) (it has {offered or 'none'})"]
    return []


def check_inpaint(models: dict[str, Any], model: str, loras: int) -> list[str]:
    """What would make darkroom refuse to inpaint on ``model`` with ``loras`` LoRAs, from its ``GET /models``.

    darkroom picks an inpainting workflow (``<family>+lora<N>+inpaint.json``) by checkpoint
    family and LoRA count, and refuses (400) a mask with none. Its example workflows inpaint
    SDXL with 0 or 1 LoRA; two need a ``sdxl+lora2+inpaint.json`` in its workflow directory.
    Pure; an empty list means nothing was found.
    """
    c = next(
        (c for c in models.get("checkpoints") or [] if isinstance(c, dict) and c.get("name") == model),
        None,
    )
    if c is None:
        return [f"{model}: darkroom has no such checkpoint"]
    if "inpaint_lora_counts" not in c:
        return [f"{model}: darkroom doesn't say whether it can inpaint (it needs inpaint_lora_counts in /models)"]
    offered = sorted(n for n in c.get("inpaint_lora_counts") or [] if isinstance(n, int))
    if loras not in offered:
        return [f"{model}: no darkroom inpainting workflow takes {loras} LoRA(s) (it has {offered or 'none'})"]
    return []


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
        priority: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.policy = policy
        self.advisory_policies = advisory_policies
        self.thinking = thinking
        self.accept_review = accept_review
        self.busy_wait = busy_wait
        self.busy_timeout = busy_timeout
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))
        # The GPU lease class darkroom asks argus-iris for (X-Argus-Priority): None is darkroom's default,
        # interactive, for a moderator or owner waiting on a draft. Sample and library runs are batch.
        self.priority = priority

    def batch(self) -> DarkroomClient:
        """This client at the ``batch`` GPU priority, sharing its connection: for sample and library runs."""
        other = copy.copy(self)
        other.priority = "batch"
        return other

    def _body(
        self, req: RenderRequest, *, for_preflight: bool = False, init: InitImage | None = None
    ) -> dict[str, Any]:
        return request_body(
            req,
            policy=self.policy,
            advisory_policies=self.advisory_policies,
            thinking=self.thinking,
            for_preflight=for_preflight,
            init=init,
        )

    # -- checks ---------------------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        resp = self.client.get(f"{self.base_url}/health")
        resp.raise_for_status()
        return resp.json()

    def models(self) -> dict[str, Any]:
        """darkroom's ``GET /models``: checkpoints (with family and LoRA counts), LoRAs, workflows."""
        try:
            # a listing, not a render: don't wait as long as a generation may take
            resp = self.client.get(f"{self.base_url}/models", timeout=_LIST_TIMEOUT)
            resp.raise_for_status()
            models = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise DarkroomError(f"darkroom's model list is unavailable: {e}") from e
        if not isinstance(models, dict):
            raise DarkroomError(f"darkroom's model list is not an object: {type(models).__name__}")
        return models

    def check_template(self, t: Template) -> list[str]:
        """:func:`check_template` against this darkroom."""
        return check_template(t, self.models())

    def preflight(self, req: RenderRequest) -> dict[str, Any]:
        """darkroom's text check alone (no render). ``outcome`` is ``"ok"`` or a refusal."""
        resp = self.client.post(f"{self.base_url}/preflight", json=self._body(req, for_preflight=True))
        if resp.status_code == 400:
            raise DarkroomError(f"darkroom refused the request: {resp.text}")
        resp.raise_for_status()
        return resp.json()

    def check_text(self, text: str, policy: str | None = None) -> tuple[bool, str]:
        """Screen text (a bio, or a teaser project's prompt or quote) with darkroom's text pre-flight.

        Under ``policy`` (a teaser project's own), else this client's. Implements
        :class:`argus_mint.bios.TextGuard`: ``(passes, detail)``.
        """
        body = {
            "user_prompt": text,
            "negative_prompt": "",
            "expand": False,
            "enforcing_policy": policy or self.policy,
            "advisory_policies": list(self.advisory_policies),
        }
        try:
            resp = self.client.post(f"{self.base_url}/preflight", json=body)
            if resp.status_code == 400:
                raise DarkroomError(f"darkroom refused the request: {resp.text}")
            resp.raise_for_status()
            verdict = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise DarkroomError(f"darkroom text check failed: {e}") from e
        if not isinstance(verdict, dict):
            raise DarkroomError(f"darkroom's text check answered with a {type(verdict).__name__}, not an object")
        if verdict.get("outcome") == "ok":
            return True, ""
        refusal = verdict.get("refusal") or {}
        return False, (refusal.get("rule") or refusal.get("reason") or verdict.get("outcome") or "refused")

    # -- generation -----------------------------------------------------------------------------

    def check_render(self, model: str, loras: int) -> list[str]:
        """:func:`check_render` against this darkroom."""
        return check_render(self.models(), model, loras)

    def check_inpaint(self, model: str, loras: int) -> list[str]:
        """:func:`check_inpaint` against this darkroom."""
        return check_inpaint(self.models(), model, loras)

    def _stream(self, req: RenderRequest, init: InitImage | None = None) -> dict[str, Any] | str:
        """Run one generation; return the record, or the error detail.

        Retries a 409 every ``busy_wait`` seconds until ``busy_timeout``. A darkroom on the GPU broker
        (argus-iris) queues a second generation itself and rarely answers 409; one without it runs one at a
        time and does, so the retry stays.
        """
        deadline = time.monotonic() + self.busy_timeout
        while True:
            headers = {"X-Argus-Priority": self.priority} if self.priority else {}
            with self.client.stream(
                "POST", f"{self.base_url}/generate/stream", json=self._body(req, init=init), headers=headers
            ) as resp:
                if resp.status_code == 409 and time.monotonic() < deadline:
                    log.info("darkroom_busy", wait=self.busy_wait)
                else:
                    if resp.status_code == 400:
                        resp.read()
                        raise DarkroomError(f"darkroom refused the request: {resp.text}")
                    resp.raise_for_status()
                    for event, data in _sse_events(resp.iter_lines()):
                        if event == "complete":
                            record = data.get("record") if isinstance(data, dict) else None
                            if not isinstance(record, dict):
                                return "darkroom's complete event carried no record"
                            return record
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
        guard = record.get("guard")
        enforced = guard.get("enforcing_policy") if isinstance(guard, dict) else None
        if enforced is None:
            return "darkroom record does not state its enforcing policy"
        if enforced != self.policy:
            return "darkroom enforced a different policy"
        if req.init_image is not None:
            got = r.get("init_image") if isinstance(r.get("init_image"), dict) else {}
            mask = got.get("mask") if isinstance(got.get("mask"), dict) else {}
            if got.get("source_sha256") != req.init_image.sha256:
                return "darkroom painted on a different input image"
            if mask.get("source_sha256") != req.init_image.mask_sha256:
                return "darkroom used a different mask"
            if got.get("denoise") != req.init_image.denoise:
                return f"darkroom used denoise {got.get('denoise')}, not {req.init_image.denoise}"
        elif r.get("init_image") is not None:
            return "darkroom painted on an input image nobody sent"
        return None

    def generate(self, req: RenderRequest, init: InitImage | None = None) -> GenResult:
        """Render ``req``; with ``init``, inpaint it into that image through its mask."""
        try:
            outcome = self._stream(req, init)
        except httpx.HTTPError as e:
            return GenResult.error(f"darkroom unreachable or failed: {e}")
        except ValueError as e:  # an event whose data isn't JSON
            return GenResult.error(f"darkroom sent a malformed event: {e}")
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
        r = record.get("request") or {}
        workflow = {"name": r["workflow"], "sha256": r.get("workflow_sha256") or ""} if r.get("workflow") else None
        version = record.get("darkroom_version")
        return GenResult.ok(img.content, ref=rid, workflow=workflow, version=str(version) if version else None)
