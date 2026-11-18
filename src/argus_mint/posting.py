"""After a teaser is approved: its posts, the anchors of what was posted, and the export.

All of this is optional and comes after review. A teaser (or a card) can be posted, published,
both or neither; nothing here changes the draft's image or its reviews.

- **Posts** (:class:`argus_mint.cards.Post`) are kept beside the draft, in ``posts.json``,
  recorded through ``POST /drafts/{id}/posted`` by argus-herald (the poster token) or the
  moderator. A post names the sha256 of what was posted, which must be the draft's image.
- **Anchors**: ``argus-mint teasers anchor`` builds a Merkle tree over the image sha256s of a
  project's posted teasers that no earlier anchor holds, and anchors its root in the Ledger. Each
  leaf's value is the 32-byte sha256 of the posted PNG (the reviewed image, byte for byte); the
  leaf hash is OpenZeppelin's ``StandardMerkleTree`` ``["bytes32"]`` one, as for any anchor
  (:mod:`argus_mint.merkle`). The tree and an :class:`AnchorRecord` are kept under the anchors
  directory (``ARGUS_MINT_ANCHORS_DIR``), one directory per project, outside any repository.
- **Export**: ``argus-mint export`` writes each teaser's reviewed PNG and its record.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from argus_mint import merkle
from argus_mint.cards import CardDraft, DraftError, DraftStore, Post, TeaserRecord, sha256_hex, write_atomic

__all__ = [
    "ANCHORS_FILE",
    "AnchorError",
    "AnchorLeaf",
    "AnchorRecord",
    "AnchorStore",
    "TeaserDraft",
    "anchor_leaves",
    "approved_at",
    "export_teasers",
    "is_current",
    "teaser_drafts",
]

ANCHORS_FILE = "anchors.json"
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class AnchorError(ValueError):
    """An anchor can't be made or recorded as asked."""


class TeaserDraft:
    """A teaser draft as read from the store: its id, record, status, posts and image."""

    def __init__(self, draft_id: str, draft: CardDraft, status: str, posts: tuple[Post, ...], image: bytes):
        self.id, self.draft, self.status, self.posts, self.image = draft_id, draft, status, posts, image

    @property
    def teaser(self) -> TeaserRecord:
        assert self.draft.teaser is not None
        return self.draft.teaser

    @property
    def approved(self) -> bool:
        return self.status in ("approved", "published")


def teaser_drafts(store: DraftStore, project: str | None = None) -> list[TeaserDraft]:
    """Every readable teaser draft (of ``project``, when given), newest first by ``created_at``."""
    out = []
    for draft_id in store.ids():
        try:
            d, _art, image = store.load(draft_id)
            if d.kind != "teaser" or d.teaser is None or (project is not None and d.teaser.project.id != project):
                continue
            status = "published" if store.published(draft_id) else d.status
            out.append(TeaserDraft(draft_id, d, status, store.posts(draft_id), image))
        except DraftError:
            continue
    return sorted(out, key=lambda t: (t.draft.created_at, t.id), reverse=True)


def is_current(t: TeaserRecord, links: Any) -> bool:
    """Whether a teaser draft was made from the line, identity and scene ``links`` (the project's
    current teaser links, or None) name for its number."""
    link = links.teasers.get(t.number) if links is not None else None
    if link is None:
        return False
    made = ((t.line.id, t.line.version), (t.identity.id, t.identity.version), (t.scene.id, t.scene.version))
    return made == (link.line.key, link.identity.key, link.scene.key)


def approved_at(d: CardDraft) -> datetime | None:
    """When the current image's last approval was given (None while it isn't approved)."""
    if d.status != "approved":
        return None
    return max(r.at for r in d.reviews if r.image_sha256 == d.image_sha256 and r.decision == "approved")


# -- anchors ---------------------------------------------------------------------------------------


class AnchorLeaf(BaseModel):
    """One leaf of a teaser anchor: the posted image's sha256, and the draft it came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    draft: str
    number: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AnchorRecord(BaseModel):
    """A Merkle root of posted teasers, anchored in the Ledger, and where its tree is kept."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    epoch: int = Field(ge=0)
    root: str = Field(pattern=r"^0x[0-9a-f]{64}$")
    count: int = Field(ge=1)
    leaves: tuple[AnchorLeaf, ...]
    tree: str  # the standard-v1 dump, written before the transaction was sent
    network: str
    chain_id: int
    ledger: str
    tx: str
    block: int
    sender: str
    anchored_at: datetime  # when it was recorded here (the block has its own timestamp)


class AnchorStore:
    """``<root>/<project>/anchors.json`` (every anchor of the project) and the trees beside it."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def dir(self, project: str) -> Path:
        if not _PROJECT.fullmatch(project):
            raise AnchorError(f"not a project id: {project!r}")
        return self.root / project

    def default_tree(self, project: str, epoch: int) -> Path:
        return self.dir(project) / f"epoch-{epoch}-tree.json"

    def load(self, project: str) -> tuple[AnchorRecord, ...]:
        f = self.dir(project) / ANCHORS_FILE
        if not f.is_file():
            return ()
        try:
            return tuple(AnchorRecord.model_validate(a) for a in json.loads(f.read_text(encoding="utf-8"))["anchors"])
        except (ValueError, KeyError, TypeError, ValidationError) as e:
            raise AnchorError(f"{f}: {e}") from e

    def add(self, project: str, record: AnchorRecord) -> None:
        records = (*self.load(project), record)
        d = self.dir(project)
        d.mkdir(parents=True, exist_ok=True)
        doc = {"anchors": [r.model_dump(mode="json") for r in records]}
        write_atomic(d / ANCHORS_FILE, json.dumps(doc, indent=2) + "\n")


def anchor_leaves(drafts: list[TeaserDraft], anchored: tuple[AnchorRecord, ...]) -> list[AnchorLeaf]:
    """The next batch: each posted, approved teaser whose image no earlier anchor holds, by number
    then age. An image posted from two drafts (the same bytes) is one leaf."""
    held = {leaf.sha256 for a in anchored for leaf in a.leaves}
    out: dict[str, AnchorLeaf] = {}
    for t in sorted(drafts, key=lambda t: (int(t.teaser.number[1:]), t.draft.created_at, t.id)):
        sha = t.draft.image_sha256
        if t.approved and t.posts and sha not in held and sha not in out:
            out[sha] = AnchorLeaf(draft=t.id, number=t.teaser.number, sha256=sha)
    return list(out.values())


def leaf_value(leaf: AnchorLeaf) -> bytes:
    """The 32-byte value a leaf commits to: the image's sha256 itself."""
    return bytes.fromhex(leaf.sha256)


# -- export ----------------------------------------------------------------------------------------


def _anchors_of(sha: str, anchors: tuple[AnchorRecord, ...]) -> list[dict[str, Any]]:
    """Each anchor holding ``sha``, with the leaf's proof when its tree can be read."""
    out = []
    for a in anchors:
        if not any(leaf.sha256 == sha for leaf in a.leaves):
            continue
        entry: dict[str, Any] = {
            k: v for k, v in a.model_dump(mode="json").items() if k not in ("leaves",)
        }  # fmt: skip
        try:
            tree = merkle.load(json.loads(Path(a.tree).read_text(encoding="utf-8")))
            index = list(tree.values).index(bytes.fromhex(sha))
            entry["proof"] = ["0x" + p.hex() for p in tree.proof(index)]
        except (OSError, ValueError, KeyError, TypeError):
            entry["proof"] = None  # the tree file isn't where the record says
        out.append(entry)
    return out


def _pick(drafts: list[TeaserDraft]) -> TeaserDraft:
    """The draft to export for a number: the newest posted one, else the newest approved."""
    return sorted(drafts, key=lambda t: (bool(t.posts), t.draft.created_at, t.id), reverse=True)[0]


def export_record(t: TeaserDraft, anchors: tuple[AnchorRecord, ...], background: Any = None) -> dict[str, Any]:
    """``T<n>.json``: what the image was made from and what happened to it since."""
    d, req = t.draft, t.draft.request
    if sha256_hex(t.image) != d.image_sha256:
        raise DraftError(f"draft {t.id}'s image on disk differs from the one reviewed")
    return {
        "format": 1,
        "project": t.teaser.project.id,
        "number": t.teaser.number,
        "draft": t.id,
        "status": t.status,
        "name": d.name,
        "image": {"file": f"{t.teaser.number}.png", "sha256": d.image_sha256},
        "art_sha256": d.art_sha256,
        "teaser": t.teaser.model_dump(mode="json"),
        "frame": d.frame.model_dump(mode="json") if d.frame else None,
        "texts": dict(d.texts),
        "render": {
            "template": {
                "id": req.template_id,
                "version": req.template_version,
                "hash": "0x" + req.template_hash.hex(),
            },
            "seed": req.seed,
            "seeds": [a.get("seed") for a in d.attempts],
            "checkpoint": req.model,
            "loras": [x.model_dump(mode="json") for x in req.loras],
            "init_image": req.init_image.model_dump(mode="json") if req.init_image else None,
            "attempts": list(d.attempts),
        },
        "background": None
        if background is None
        else {
            "id": background.id,
            "sha256": background.sha256,
            "seed": background.request.seed,
            "seeds": [a.get("seed") for a in background.attempts],
            "checkpoint": background.request.model,
            "loras": [x.model_dump(mode="json") for x in background.request.loras],
            "made_with": dict(background.made_with),
        },
        "made_with": dict(d.made_with),
        "reviews": [r.model_dump(mode="json") for r in d.reviews],
        "posts": [p.model_dump(mode="json") for p in t.posts],
        "anchors": _anchors_of(d.image_sha256, anchors),
    }


def export_teasers(
    drafts: list[TeaserDraft],
    anchors: tuple[AnchorRecord, ...],
    out: Path,
    backgrounds: Any = None,
) -> list[dict[str, Any]]:
    """Write ``T<n>.png`` (the reviewed bytes) and ``T<n>.json`` for each approved teaser number.

    ``backgrounds`` (a :class:`argus_mint.backgrounds.BackgroundStore`, optional) adds each
    background's seeds and checkpoint to the record. Returns a summary per teaser.
    """
    by_number: dict[str, list[TeaserDraft]] = {}
    for t in drafts:
        if t.approved:
            by_number.setdefault(t.teaser.number, []).append(t)
    if not by_number:
        raise DraftError("no approved teaser to export")
    out.mkdir(parents=True, exist_ok=True)
    summary = []
    for number in sorted(by_number, key=lambda n: int(n[1:])):
        t = _pick(by_number[number])
        bg = None
        if backgrounds is not None:
            try:
                _s, bg = backgrounds.find(t.teaser.project.id, t.teaser.background.id)
            except ValueError:
                bg = None
        record = export_record(t, anchors, bg)
        (out / f"{number}.png").write_bytes(t.image)
        (out / f"{number}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        summary.append({"number": number, "draft": t.id, "sha256": t.draft.image_sha256, "posts": len(t.posts)})
    return summary
