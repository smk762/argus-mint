"""Card bios: short flavour text written by a language model from a reviewed bio template.

A bio template is reviewed like a render template (hard rule 2): ``draft | approved |
retired``, approval recorded in the file. Its prompt may use the render template's
variables, which come from fixed lists, and nothing else. The card's name is free text, so
it never goes to the model (hard rule 1): the model is told to write a literal ``{name}``
token, and argus-mint puts the real name in afterwards. That also stops the model inventing
some other name, and stops a player's name being used as a prompt injection.

Every bio is screened before a person sees it. The text, still with its ``{name}`` token, is
checked by a :class:`TextGuard` (darkroom's text pre-flight). A flagged bio is rewritten,
up to ``max_attempts`` times, and every attempt is recorded (hard rule 3's "retry, don't
loosen", applied to text). The moderator and the owner then see the bio on the card image
when they review the draft (hard rule 9).

Bios are not reproducible: the model is sampled without a seed. The exact text is kept in
the draft, drawn on the card, and published as the card's ``description``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, Protocol

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from argus_mint._codec import canonical, keccak256
from argus_mint.metadata import BioRef
from argus_mint.templates import Template, placeholders

__all__ = [
    "NAME_TOKEN",
    "WRITER_KEY_ENV",
    "Bio",
    "BioAttempt",
    "BioError",
    "BioFailed",
    "BioTemplate",
    "ChatWriter",
    "TextGuard",
    "Writer",
    "load_bio",
    "write_bio",
]

WRITER_KEY_ENV = "ARGUS_MINT_WRITER_KEY"  # bearer token for the bio writer, if it needs one
NAME_TOKEN = re.compile(r"\{\s*name\s*\}", re.IGNORECASE)
_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
_SENTENCE_END = re.compile(r"[.!?…](?=\s|$)")


class BioError(ValueError):
    """A bio template is invalid, doesn't fit the render template, or the writer failed."""


class BioFailed(BioError):
    """Every attempt was rejected. ``attempts`` holds them all."""

    def __init__(self, message: str, attempts: tuple[BioAttempt, ...]):
        super().__init__(message)
        self.attempts = attempts


class BioTemplate(BaseModel):
    """One version of a bio template, as reviewed and recorded in its YAML file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=_ID_PATTERN)
    version: int = Field(ge=1)
    status: Literal["draft", "approved", "retired"]
    approved_by: str | None = None
    approved_at: date | None = None
    model: str = Field(min_length=1)
    system: str = Field(min_length=1)
    prompt: str = Field(min_length=1)  # {variable} placeholders; write the name token as {{name}}
    temperature: float = Field(default=0.9, ge=0.0, le=2.0)
    max_tokens: int = Field(default=200, ge=16, le=2000)
    max_chars: int = Field(default=400, ge=40, le=4000)
    max_attempts: int = Field(default=4, ge=1, le=16)

    @model_validator(mode="after")
    def _check(self) -> BioTemplate:
        if placeholders(self.system):
            raise ValueError("the system prompt must not contain placeholders (write the name token as {{name}})")
        names = placeholders(self.prompt)
        if "name" in names:
            raise ValueError("the name never goes to the model: write the token as {{name}}, not {name}")
        if self.status != "draft" and (not self.approved_by or self.approved_at is None):
            raise ValueError(f"status {self.status!r} requires approved_by and approved_at (approval is a human step)")
        return self

    def hash(self) -> bytes:
        """keccak256 of the canonical template, excluding ``status`` (as for render templates)."""
        return keccak256(canonical(self.model_dump(mode="json", exclude={"status"})))

    def ref(self) -> BioRef:
        return BioRef(id=self.id, version=self.version, hash=self.hash(), model=self.model)

    def fill(self, values: dict[str, str], render_template: Template) -> str:
        """The user prompt for one card. ``values`` are the render's variables (fixed-list values)."""
        names = set(placeholders(self.prompt))
        missing = names - render_template.variables.keys()
        if missing:
            raise BioError(
                f"bio {self.id} v{self.version} uses {sorted(missing)}, which template "
                f"{render_template.id} v{render_template.version} doesn't have"
            )
        for k in names:
            if values.get(k) not in render_template.variables[k]:
                raise BioError(f"{k}={values.get(k)!r} is not in the template's list")
        return self.prompt.format(**{k: values[k] for k in names})


def load_bio(path: Path) -> BioTemplate:
    """Load and validate one bio template YAML file."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise BioError(f"{path}: {e}") from e
    if not isinstance(raw, dict):
        raise BioError(f"{path}: expected a mapping at the top level")
    try:
        return BioTemplate.model_validate(raw)
    except ValidationError as e:
        raise BioError(f"{path}: {e}") from e


class Writer(Protocol):
    def write(self, *, model: str, system: str, prompt: str, temperature: float, max_tokens: int) -> str: ...


class TextGuard(Protocol):
    def check_text(self, text: str) -> tuple[bool, str]:
        """(passes, detail). ``detail`` explains a refusal."""
        ...


class ChatWriter:
    """A :class:`Writer` over an OpenAI-style ``POST {base_url}/chat`` (messages in, choices out)."""

    def __init__(
        self, base_url: str, *, api_key: str | None = None, client: httpx.Client | None = None, timeout: float = 180
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=timeout)

    def write(self, *, model: str, system: str, prompt: str, temperature: float, max_tokens: int) -> str:
        body = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            r = self.client.post(f"{self.base_url}/chat", json=body, headers=headers)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
            raise BioError(f"the bio writer at {self.base_url} failed: {e}") from e
        if not isinstance(content, str) or not content.strip():
            raise BioError("the bio writer returned no text")
        return content


@dataclass(frozen=True)
class BioAttempt:
    outcome: Literal["ok", "rejected", "unusable"]
    detail: str = ""
    text: str = ""


@dataclass(frozen=True)
class Bio:
    text: str  # with the real name in place
    template: BioTemplate
    attempts: tuple[BioAttempt, ...] = field(default=())


def clean(raw: str, max_chars: int) -> str:
    """Tidy a model's reply: one paragraph, no wrapping quotes, cut at a sentence end if too long."""
    text = " ".join(raw.split()).strip().strip('"“”').strip()
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    ends = [m.end() for m in _SENTENCE_END.finditer(head)]
    if ends and ends[-1] >= max_chars // 3:
        return head[: ends[-1]]
    return head.rsplit(" ", 1)[0].rstrip(",;:—-") + "…"


def write_bio(
    writer: Writer,
    guard: TextGuard,
    bt: BioTemplate,
    values: dict[str, str],
    render_template: Template,
    name: str,
    *,
    review: bool = False,
) -> Bio:
    """Write and screen a bio for one card, retrying a flagged or unusable reply.

    ``review=True`` allows a draft bio template (for ``argus-mint sample``); cards need an
    approved one. Raises :class:`BioFailed` when every attempt is rejected, and
    :class:`BioError` at once if the writer fails.
    """
    if bt.status != "approved" and not review:
        raise BioError(f"bio {bt.id} v{bt.version} is {bt.status}; only approved bio templates go on a card")
    prompt = bt.fill(values, render_template)
    attempts: list[BioAttempt] = []
    for _ in range(bt.max_attempts):
        raw = writer.write(
            model=bt.model,
            system=bt.system.format(),
            prompt=prompt,
            temperature=bt.temperature,
            max_tokens=bt.max_tokens,
        )
        text = clean(NAME_TOKEN.sub("{name}", raw), bt.max_chars)
        stray = re.sub(r"\{name\}", "", text)
        if "{" in stray or "}" in stray or not text:
            attempts.append(BioAttempt("unusable", "stray braces or empty", text))
            continue
        ok, detail = guard.check_text(text)  # still holds the {name} token: the name never reaches a model
        if not ok:
            attempts.append(BioAttempt("rejected", detail, text))
            continue
        attempts.append(BioAttempt("ok", "", text))
        return Bio(text=text.replace("{name}", name), template=bt, attempts=tuple(attempts))
    raise BioFailed(f"bio {bt.id} v{bt.version}: all {bt.max_attempts} attempts were rejected", tuple(attempts))
