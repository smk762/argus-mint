"""Template registry: load and validate reviewed render templates, and fill a template's
variables into a render request.

A render is never a free-text prompt (hard rule 1): it is a template id + version plus one
value per placeholder, each picked from that template's fixed list. Only ``approved``
templates fill (hard rule 2). Pure apart from reading the YAML files.
"""

from __future__ import annotations

import itertools
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


class LoraRef(BaseModel):
    """A LoRA by file name, with its weight and the trigger words the renderer appends.

    A bare string in YAML means ``{name: <string>, weight: 1.0}``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    weight: float = Field(default=1.0, ge=-4.0, le=4.0)
    trigger_words: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _from_name(cls, v: object) -> object:
        return {"name": v} if isinstance(v, str) else v

    @field_validator("trigger_words")
    @classmethod
    def _check_triggers(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for w in v:
            if not w.strip() or "{" in w or "}" in w or "\n" in w:
                raise ValueError(f"trigger word {w!r} is blank, multi-line or contains braces")
        return v


class LoraOption(BaseModel):
    """A LoRA a card may choose (reviewed with the template), at one of its allowed weights.

    ``models``: the checkpoints it may pair with (a LoRA belongs to one family); empty means any
    the template allows.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    weights: tuple[float, ...] = (1.0,)
    trigger_words: tuple[str, ...] = ()
    models: tuple[str, ...] = ()

    @field_validator("weights")
    @classmethod
    def _check_weights(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if not v or len(set(v)) != len(v) or any(not -4.0 <= w <= 4.0 for w in v):
            raise ValueError("weights: one or more distinct values in -4..4")
        return v

    _check_triggers = field_validator("trigger_words")(LoraRef._check_triggers.__func__)  # type: ignore[attr-defined]


class LoraChoice(BaseModel):
    """A render's pick from the template's ``lora_options``. ``weight`` defaults to the first allowed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    weight: float | None = None


class Sampling(BaseModel):
    """Sampler settings, fixed by the reviewed template so a render is reproducible."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sampler: str = Field(min_length=1)
    scheduler: str = Field(min_length=1)
    steps: int = Field(gt=0, le=200)
    cfg: float = Field(gt=0, le=30)
    clip_skip: int = Field(default=1, ge=1, le=12)
    guidance: float | None = Field(default=None, gt=0, le=30)  # Flux's distilled guidance; others leave it unset


def family(model: str) -> str:
    """A checkpoint's family: its top-level directory under darkroom's models root (``sdxl``, ``flux``…)."""
    return model.split("/", 1)[0] if "/" in model else ""


class Template(BaseModel):
    """One version of a render template, as reviewed and recorded in its YAML file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=_ID_PATTERN)
    version: int = Field(ge=1)
    status: Literal["draft", "approved", "retired"]
    approved_by: str | None = None
    approved_at: date | None = None
    model: str = Field(min_length=1)  # the default checkpoint
    # Other checkpoints a render may choose instead (reviewed with the template). Empty: only `model`.
    models: tuple[str, ...] = ()
    loras: tuple[LoraRef, ...] = ()  # always applied
    # LoRAs a card may add (reviewed), up to max_loras of them. Empty: none to choose.
    lora_options: tuple[LoraOption, ...] = ()
    max_loras: int = Field(default=1, ge=0, le=4)
    sampling: Sampling | None = None  # required by darkroom; optional for other renderers
    # Sampling for checkpoints of another family (keyed by family: "flux", "sd35"...), when it differs.
    sampling_by_family: dict[str, Sampling] = Field(default_factory=dict)
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
        names = [o.name for o in self.lora_options]
        if len(set(names)) != len(names):
            raise ValueError("a LoRA option appears twice")
        allowed = (self.model, *self.models)
        for o in self.lora_options:
            stray = [m for m in o.models if m not in allowed]
            if stray:
                raise ValueError(f"LoRA option {o.name!r} pairs with checkpoints the template doesn't allow: {stray}")
        return self

    @property
    def key(self) -> tuple[str, int]:
        return (self.id, self.version)

    def hash(self) -> bytes:
        """keccak256 of the template's canonical JSON, excluding ``status``.

        This is the ``template_hash`` a card's provenance carries. ``status`` is left out so
        retiring a template doesn't change the hash of cards it already made.
        """
        # Fields added later are left out while unused, so templates from before them keep their hash.
        doc = self.model_dump(mode="json", exclude={"status"})
        for key, unused in (
            ("models", not self.models),
            ("lora_options", not self.lora_options),
            ("max_loras", not self.lora_options),
            ("sampling_by_family", not self.sampling_by_family),
        ):
            if unused:
                doc.pop(key, None)
        for sampling in [doc.get("sampling"), *doc.get("sampling_by_family", {}).values()]:
            if isinstance(sampling, dict) and sampling.get("guidance") is None:
                sampling.pop("guidance", None)
        return keccak256(canonical(doc))

    def sampling_for(self, model: str) -> Sampling | None:
        """The sampling for ``model``: its family's, if the template sets one, else the default."""
        return self.sampling_by_family.get(family(model), self.sampling)

    def resolve_loras(self, choices: tuple[LoraChoice, ...] | list[LoraChoice], model: str) -> tuple[LoraRef, ...]:
        """The template's own LoRAs plus the chosen options, as the renderer gets them.

        Refuses a choice that isn't an option, a weight it doesn't allow, a checkpoint it can't
        pair with, a repeat, or more than ``max_loras``.
        """
        options = {o.name: o for o in self.lora_options}
        if len(choices) > self.max_loras:
            raise TemplateError(f"at most {self.max_loras} LoRA(s) may be chosen")
        picked: list[LoraRef] = []
        for c in choices:
            option = options.get(c.name)
            if option is None:
                raise TemplateError(f"LoRA {c.name!r} is not one this template offers: {sorted(options)}")
            weight = option.weights[0] if c.weight is None else c.weight
            if weight not in option.weights:
                raise TemplateError(f"LoRA {c.name!r} allows weights {list(option.weights)}, not {weight}")
            if option.models and model not in option.models:
                raise TemplateError(f"LoRA {c.name!r} doesn't pair with {model}")
            if any(p.name == c.name for p in picked):
                raise TemplateError(f"LoRA {c.name!r} chosen twice")
            picked.append(LoraRef(name=option.name, weight=weight, trigger_words=option.trigger_words))
        return (*self.loras, *picked)

    def allowed_models(self) -> tuple[str, ...]:
        """The checkpoints a render may use: the default first, then the reviewed alternatives."""
        return (self.model, *(m for m in self.models if m != self.model))


class RenderRequest(BaseModel):
    """Everything a renderer needs, fully determined by template version + values + seed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str
    template_version: int
    template_hash: Bytes32
    model: str
    loras: tuple[LoraRef, ...]
    sampling: Sampling | None
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


def fill(
    t: Template, values: dict[str, str], seed: int, model: str | None = None, loras: tuple[LoraChoice, ...] = ()
) -> RenderRequest:
    """Fill ``values`` into an approved template.

    Raises :class:`NotApprovedError` unless ``t.status == "approved"``, and
    :class:`TemplateError` for a missing or extra variable, a value outside its list,
    or a seed outside ``0..2**32-1``.
    """
    if t.status != "approved":
        raise NotApprovedError(f"template {t.id} v{t.version} is {t.status}; only approved templates render")
    return _build(t, values, seed, model, loras)


def fill_for_review(
    t: Template, values: dict[str, str], seed: int = 0, model: str | None = None, loras: tuple[LoraChoice, ...] = ()
) -> RenderRequest:
    """Like :func:`fill` but for any status: lets a reviewer check a draft before approving it.

    Used for the text pre-flight and for sample renders (:func:`argus_mint.render.render_for_review`),
    which stay local files. Cards are rendered through :func:`argus_mint.render.render`, which
    calls :func:`fill` and so refuses non-approved templates.
    """
    return _build(t, values, seed, model, loras)


def combinations(t: Template) -> list[dict[str, str]]:
    """Every assignment of values to the template's variables (the prompts a reviewer approves)."""
    names = sorted(t.variables)
    return [dict(zip(names, combo, strict=True)) for combo in itertools.product(*(t.variables[n] for n in names))]


def _build(
    t: Template, values: dict[str, str], seed: int, model: str | None = None, loras: tuple[LoraChoice, ...] = ()
) -> RenderRequest:
    if model is not None and model not in t.allowed_models():
        raise TemplateError(f"checkpoint {model!r} is not one this template allows: {list(t.allowed_models())}")
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
        model=model or t.model,
        loras=t.resolve_loras(tuple(loras), model or t.model),
        sampling=t.sampling_for(model or t.model),
        width=t.width,
        height=t.height,
        prompt=t.prompt.format(**values),
        negative=t.negative,
        variables=dict(sorted(values.items())),
        seed=seed,
    )
