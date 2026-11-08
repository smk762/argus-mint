"""Template registry: load and validate reviewed render templates, and fill a template's
variables into a render request.

A render is never a free-text prompt (hard rule 1): it is a template id + version plus one
value per placeholder, each picked from that template's fixed list. Only ``approved``
templates fill (hard rule 2). Pure apart from reading the YAML files.
"""

from __future__ import annotations

import re
import string
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from argus_mint._codec import Bytes32, canonical, keccak256

SEED_MAX = 2**32 - 1  # the range every diffusion backend accepts
_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
_VAR_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"


class TemplateError(ValueError):
    """A template file is invalid, or a fill request doesn't match its template."""


class NotApprovedError(TemplateError):
    """The template's status is not ``approved``."""


def placeholders(text: str) -> list[str]:
    """Placeholder names in ``text`` (``{name}``), in order. ``{{``/``}}`` are literal braces.

    Format specs, conversions, attribute and index access are rejected: a placeholder is a
    bare name and nothing else.
    """
    names: list[str] = []
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as e:
        raise TemplateError(f"bad placeholder syntax: {e}") from e
    for _literal, field, spec, conversion in parsed:
        if field is None:
            continue
        if spec or conversion or not field.isidentifier():
            raise TemplateError(f"placeholder {{{field}}} must be a bare name (no format spec, conversion or access)")
        names.append(field)
    return names


class Template(BaseModel):
    """One version of a render template, as reviewed and recorded in its YAML file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=_ID_PATTERN)
    version: int = Field(ge=1)
    status: Literal["draft", "approved", "retired"]
    approved_by: str | None = None
    approved_at: date | None = None
    model: str = Field(min_length=1)
    loras: tuple[str, ...] = ()
    width: int = Field(gt=0, le=8192)
    height: int = Field(gt=0, le=8192)
    prompt: str = Field(min_length=1)
    negative: str = ""
    variables: dict[str, tuple[str, ...]]
    max_attempts: int = Field(default=4, ge=1, le=16)

    @field_validator("variables")
    @classmethod
    def _check_variables(cls, v: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        for name, values in v.items():
            if not re.match(_VAR_PATTERN, name):
                raise ValueError(f"variable name {name!r} must match {_VAR_PATTERN}")
            if not values:
                raise ValueError(f"variable {name!r} has no allowed values")
            if len(set(values)) != len(values):
                raise ValueError(f"variable {name!r} lists a value twice")
            for value in values:
                if not value.strip() or "{" in value or "}" in value:
                    raise ValueError(f"variable {name!r}: value {value!r} is blank or contains braces")
        return v

    @model_validator(mode="after")
    def _check_consistency(self) -> Template:
        in_prompt = placeholders(self.prompt)
        if len(set(in_prompt)) != len(in_prompt):
            raise ValueError("a placeholder appears more than once in the prompt")
        if placeholders(self.negative):
            raise ValueError("the negative prompt must not contain placeholders")
        missing = set(in_prompt) - self.variables.keys()
        unused = self.variables.keys() - set(in_prompt)
        if missing:
            raise ValueError(f"placeholders without a variable list: {sorted(missing)}")
        if unused:
            raise ValueError(f"variables not used in the prompt: {sorted(unused)}")
        if self.status != "draft" and (not self.approved_by or self.approved_at is None):
            raise ValueError(f"status {self.status!r} requires approved_by and approved_at (approval is a human step)")
        return self

    @property
    def key(self) -> tuple[str, int]:
        return (self.id, self.version)

    def hash(self) -> bytes:
        """keccak256 of the template's canonical JSON, excluding ``status``.

        This is the ``template_hash`` a card's provenance carries. ``status`` is left out so
        retiring a template doesn't change the hash of cards it already made.
        """
        return keccak256(canonical(self.model_dump(mode="json", exclude={"status"})))


class RenderRequest(BaseModel):
    """Everything a renderer needs, fully determined by template version + values + seed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str
    template_version: int
    template_hash: Bytes32
    model: str
    loras: tuple[str, ...]
    width: int
    height: int
    prompt: str
    negative: str
    variables: dict[str, str]
    seed: int = Field(ge=0, le=SEED_MAX)


def load_template(path: Path) -> Template:
    """Load and validate one template YAML file."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise TemplateError(f"{path}: not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise TemplateError(f"{path}: expected a mapping at the top level")
    try:
        return Template.model_validate(raw)
    except ValidationError as e:
        raise TemplateError(f"{path}: {e}") from e


def load_registry(path: Path) -> dict[tuple[str, int], Template]:
    """Load every ``*.yaml``/``*.yml`` under ``path`` (recursively), keyed by (id, version).

    All statuses are loaded (``fill`` is where non-approved templates are refused), so a
    registry can also list drafts awaiting review. Two files with the same (id, version)
    are an error.
    """
    root = Path(path)
    files = sorted([*root.rglob("*.yaml"), *root.rglob("*.yml")])
    registry: dict[tuple[str, int], Template] = {}
    origin: dict[tuple[str, int], Path] = {}
    for f in files:
        t = load_template(f)
        if t.key in registry:
            raise TemplateError(f"{f}: duplicate template {t.id} v{t.version} (also in {origin[t.key]})")
        registry[t.key] = t
        origin[t.key] = f
    return registry


def fill(t: Template, values: dict[str, str], seed: int) -> RenderRequest:
    """Fill ``values`` into an approved template.

    Raises :class:`NotApprovedError` unless ``t.status == "approved"``, and
    :class:`TemplateError` for a missing or extra variable, a value outside its list,
    or a seed outside ``0..2**32-1``.
    """
    if t.status != "approved":
        raise NotApprovedError(f"template {t.id} v{t.version} is {t.status}; only approved templates render")
    missing = t.variables.keys() - values.keys()
    extra = values.keys() - t.variables.keys()
    if missing:
        raise TemplateError(f"missing variables: {sorted(missing)}")
    if extra:
        raise TemplateError(f"unknown variables: {sorted(extra)}")
    for name, value in values.items():
        if value not in t.variables[name]:
            raise TemplateError(f"{name}={value!r} is not in the template's list {list(t.variables[name])}")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= SEED_MAX:
        raise TemplateError(f"seed must be an integer in 0..{SEED_MAX}")
    return RenderRequest(
        template_id=t.id,
        template_version=t.version,
        template_hash=t.hash(),
        model=t.model,
        loras=t.loras,
        width=t.width,
        height=t.height,
        prompt=t.prompt.format(**values),
        negative=t.negative,
        variables=dict(sorted(values.items())),
        seed=seed,
    )
