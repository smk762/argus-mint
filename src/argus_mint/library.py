"""A library of pre-rendered portraits: cards for a demo without a GPU.

``argus-mint library build`` renders a template through darkroom, the usual safety check and
retries included, for a set of variable combinations across several checkpoints. The same
combination uses the same seed on every checkpoint, so the images compare like for like.
Everything lands in a directory: ``manifest.json`` and one PNG per entry, exactly as darkroom
returned it (its sha256 is darkroom's record of the image).

A draft may then take its art from an entry instead of rendering. That is honest only if the
entry is what the card's template would render now, so :func:`check_entry` requires the
approved template, given the entry's variables, seed and checkpoint, to produce the very
request the entry was rendered from (template hash aside: approving a draft template changes its
hash, not its prompt). The draft then records the entry, and its provenance is that of a render.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import random
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from argus_mint.render import Darklight, RenderError, RenderFailed, render_for_review
from argus_mint.templates import RenderRequest, Template, TemplateError, fill

__all__ = ["Library", "LibraryEntry", "LibraryError", "build", "check_entry", "model_slug", "pick_variable_sets"]

MANIFEST = "manifest.json"
_ENTRY_ID = re.compile(r"^[0-9]{3}-[a-z0-9-]{1,60}$")


class LibraryError(ValueError):
    """The library is missing, malformed, or an entry doesn't fit the template."""


class LibraryEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str  # "<group>-<checkpoint slug>", e.g. "007-juggernautxl-ragnarokby"
    group: int = Field(ge=0)  # entries in a group share variables and seed: one per checkpoint
    model: str
    variables: dict[str, str]
    seed: int  # the seed asked for (the group's)
    request: RenderRequest  # the request that produced the image (its seed is the final one)
    attempts: tuple[dict[str, Any], ...]
    file: str  # relative to the library directory
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: int = 1
    template: str
    template_version: int
    models: list[str]
    count: int
    seed: int
    created_at: datetime
    entries: list[LibraryEntry] = Field(default_factory=list)
    # Combinations that made no image. `rejected`: the safety check refused every attempt (final);
    # otherwise an error stopped the render, and a resumed build tries it again.
    failed: list[dict[str, Any]] = Field(default_factory=list)


def model_slug(model: str) -> str:
    """``sdxl/juggernautXL_ragnarokBy/juggernautXL_ragnarokBy.safetensors`` -> ``juggernautxl-ragnarokby``."""
    stem = Path(model).stem
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:60]


def pick_variable_sets(t: Template, count: int, seed: int) -> list[dict[str, str]]:
    """``count`` diverse combinations, deterministic for ``seed``.

    Every variable cycles through its whole list before repeating (shuffled per variable), so
    48 portraits of a 16-role template show every role three times, every gender sixteen, and
    so on, without a combination repeating. Raises :class:`LibraryError` if the template has
    fewer than ``count`` combinations.
    """
    possible = math.prod(len(values) for values in t.variables.values())
    if count > possible:
        raise LibraryError(f"template {t.id} v{t.version} has only {possible} combinations, not {count} different ones")
    rng = random.Random(seed)
    names = sorted(t.variables)
    columns: dict[str, list[str]] = {}
    for name in names:
        values = list(t.variables[name])
        column: list[str] = []
        while len(column) < count:
            rng.shuffle(values)
            column += values
        columns[name] = column[:count]
    picked: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    for i in range(count):
        combo = {n: columns[n][i] for n in names}
        key = tuple(combo[n] for n in names)
        while key in seen:  # rare with several variables; nudge one value
            n = rng.choice(names)
            combo[n] = rng.choice(t.variables[n])
            key = tuple(combo[k] for k in names)
        seen.add(key)
        picked.append(combo)
    return picked


class Library:
    """A built library directory."""

    def __init__(self, root: Path):
        self.root = Path(root)
        try:
            self.manifest = Manifest.model_validate_json((self.root / MANIFEST).read_text(encoding="utf-8"))
        except OSError as e:
            raise LibraryError(f"no library at {self.root}: {e}") from e
        except ValueError as e:
            raise LibraryError(f"{self.root / MANIFEST}: {e}") from e
        self._by_id = {e.id: e for e in self.manifest.entries}

    @classmethod
    def maybe(cls, root: Path | None) -> Library | None:
        if root is None or not (Path(root) / MANIFEST).is_file():
            return None
        return cls(root)

    @property
    def entries(self) -> list[LibraryEntry]:
        return self.manifest.entries

    def entry(self, entry_id: str) -> LibraryEntry:
        if not _ENTRY_ID.match(entry_id) or entry_id not in self._by_id:
            raise LibraryError(f"no library entry {entry_id!r}")
        return self._by_id[entry_id]

    def image(self, entry_id: str) -> bytes:
        e = self.entry(entry_id)
        try:
            data = (self.root / e.file).read_bytes()
        except OSError as err:
            raise LibraryError(f"library image {e.file} can't be read: {err}") from err
        if hashlib.sha256(data).hexdigest() != e.sha256:
            raise LibraryError(f"library image {e.file} differs from the one rendered")
        return data

    def thumbnail(self, entry_id: str, width: int = 384) -> bytes:
        """A JPEG preview (cached beside the image). Never used for a card."""
        e = self.entry(entry_id)
        cached = self.root / "thumbs" / f"{e.id}-{width}.jpg"
        if cached.is_file():
            return cached.read_bytes()
        from PIL import Image

        with Image.open(io.BytesIO(self.image(entry_id))) as im:
            im = im.convert("RGB")
            im.thumbnail((width, width * 4))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=82, optimize=True)
        # Cache by rename, so a concurrent request never reads half a file. A library that can't
        # be written to (a read-only mount) is still served, just uncached.
        tmp = cached.with_name(f".{cached.name}.{secrets.token_hex(4)}.tmp")
        try:
            cached.parent.mkdir(exist_ok=True)
            tmp.write_bytes(buf.getvalue())
            os.replace(tmp, cached)
        except OSError:
            tmp.unlink(missing_ok=True)
        return buf.getvalue()


def check_entry(entry: LibraryEntry, t: Template) -> RenderRequest:
    """The request ``t`` would render for this entry; refuses an entry it wouldn't have produced.

    ``t`` must be approved (``fill`` refuses otherwise). Everything in the request must match
    the entry's except the template hash.
    """
    try:
        req = fill(t, entry.variables, entry.request.seed, entry.model)
    except TemplateError as e:
        raise LibraryError(f"library entry {entry.id} doesn't fit template {t.id} v{t.version}: {e}") from e
    ours = req.model_dump(mode="json", exclude={"template_hash"})
    theirs = entry.request.model_dump(mode="json", exclude={"template_hash"})
    if ours != theirs:
        differ = sorted(k for k in ours if ours[k] != theirs.get(k))
        raise LibraryError(f"library entry {entry.id} was rendered from a different request ({', '.join(differ)})")
    return req


def build(
    client: Darklight,
    t: Template,
    root: Path,
    *,
    models: list[str] | None = None,
    count: int = 48,
    seed: int = 0,
    progress: Callable[[str], None] = lambda _m: None,
) -> Library:
    """Render the library into ``root``, model by model (a checkpoint loads once), resumably.

    An entry already in the manifest (with its file) is kept, so an interrupted build continues.
    A combination every attempt was rejected for is recorded under ``failed`` and skipped for
    good. One that stopped on an error (darkroom unreachable, a failed job) is recorded there
    too, and tried again when the build is resumed.
    """
    root = Path(root)
    (root / "images").mkdir(parents=True, exist_ok=True)
    models = list(models or t.allowed_models())
    unknown = [m for m in models if m not in t.allowed_models()]
    if unknown:
        raise LibraryError(f"template {t.id} v{t.version} doesn't allow {unknown}")
    sets = pick_variable_sets(t, count, seed)
    path = root / MANIFEST
    manifest = (
        Manifest.model_validate_json(path.read_text(encoding="utf-8"))
        if path.is_file()
        else Manifest(
            template=t.id,
            template_version=t.version,
            models=models,
            count=count,
            seed=seed,
            created_at=datetime.now(UTC),
        )  # fmt: skip
    )
    if (manifest.template, manifest.template_version, manifest.count, manifest.seed) != (t.id, t.version, count, seed):
        raise LibraryError(f"{path} was started for other settings; use a new directory")
    manifest.models = list(dict.fromkeys([*manifest.models, *models]))
    slugs: dict[str, str] = {}
    for m in manifest.models:  # the slug is the entry id and the file name: two checkpoints can't share one
        slug = model_slug(m)
        if not slug:
            raise LibraryError(f"checkpoint {m!r} has no letters or digits to name its entries by")
        if slugs.setdefault(slug, m) != m:
            raise LibraryError(
                f"checkpoints {slugs[slug]!r} and {m!r} would share entry ids ({slug}-); use two directories"
            )
    # An entry whose file is gone is rendered again, so it leaves the manifest: no id is listed twice.
    manifest.entries = [e for e in manifest.entries if (root / e.file).is_file()]
    done = {e.id for e in manifest.entries}
    # Only a safety rejection is final. An error is tried again (as is a failure recorded before
    # the two were told apart).
    manifest.failed = [f for f in manifest.failed if f.get("rejected", False)]
    failed = {(f["group"], f["model"]) for f in manifest.failed}

    def save() -> None:
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(manifest.model_dump_json(indent=1) + "\n", encoding="utf-8")
        tmp.replace(path)

    total = len(models) * len(sets)
    n = 0
    for model in models:
        slug = model_slug(model)
        for group, values in enumerate(sets):
            n += 1
            entry_id = f"{group:03d}-{slug}"
            if entry_id in done or (group, model) in failed:
                continue
            group_seed = (seed * 1000 + group) % (2**32)
            try:
                r = render_for_review(client, t, values, group_seed, model)
            except RenderError as e:
                manifest.failed.append(
                    {
                        "group": group,
                        "model": model,
                        "variables": values,
                        "error": str(e),
                        "rejected": isinstance(e, RenderFailed),
                    }
                )
                save()
                progress(f"[{n}/{total}] {entry_id}: {e}")
                continue
            file = f"images/{entry_id}.png"
            (root / file).write_bytes(r.image)
            manifest.entries.append(
                LibraryEntry(
                    id=entry_id,
                    group=group,
                    model=model,
                    variables=values,
                    seed=group_seed,
                    request=r.request,
                    attempts=tuple(a.to_json() for a in r.attempts),
                    file=file,
                    sha256=hashlib.sha256(r.image).hexdigest(),
                )  # fmt: skip
            )
            save()
            progress(f"[{n}/{total}] {entry_id} ({len(r.attempts)} attempt{'s' if len(r.attempts) > 1 else ''})")
    save()
    return Library(root)


def describe(lib: Library) -> dict[str, Any]:
    """A short summary for the CLI."""
    m = lib.manifest
    return {
        "template": f"{m.template} v{m.template_version}",
        "models": m.models,
        "groups": m.count,
        "entries": len(m.entries),
        "failed": len(m.failed),
        "directory": str(lib.root),
    }


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)
