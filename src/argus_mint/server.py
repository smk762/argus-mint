"""``argus-mint serve``: the card workflow over HTTP, for argus-studio and argus-assay.

=====================================  =============================================================================
``GET  /health``                       version, wire version, network, Card, capabilities, which backends answer
``GET  /templates`` ``/frames`` ``/bios``  the reviewed files (every status), with their hashes
``GET  /frames/{id}/{version}/overlay``  a frame's overlay PNG
``GET  /drafts``                       newest first; ``?status=pending|approved|rejected|published``, paged
``GET  /drafts/{id}``                  the draft, its status, and the publish result once published
``GET  /drafts/{id}/image`` ``/art``   the card image (what reviewers approve) and the unframed render
``POST /drafts``                       render, write the bio, frame: a new pending draft (moderator token)
``POST /drafts/{id}/challenge``        the SIWE message the owner signs to approve or reject
``POST /drafts/{id}/review``           a decision: moderator by token, owner by signed challenge
``POST /drafts/{id}/publish``          pin and sign an approved draft (moderator token)
``POST /drafts/{id}/text``             change the card's name, bio or display text: redrawn, reviewed again
``POST /samples`` ``GET /samples[/{id}]``  sample cards of a draft template/frame/bio, as background jobs
``GET  /samples/{id}/{n}/art|framed``  a sample's images (local only: never pinned, signed or minted)
``POST /frames/{id}/{version}/preview``  a frame (any status) on a draft's or sample's art, as a PNG
``POST /{templates,frames,bios}/{id}/{version}/review``  approve a draft or retire an approved file
=====================================  =============================================================================

Who may do what: the moderator token (``ARGUS_MINT_MODERATOR_TOKEN``) creates drafts, reviews
as the moderator and publishes. The owner reviews by signing an EIP-4361 message with the
wallet the card is minted to; the message names the draft, the decision and the image's
sha256, and its nonce works once. Neither role can act as the other.

Reads are open to the configured CORS origins. Writes from any other browser origin are
refused (cortex's ``WriteGuard``: CORS alone is not a write boundary), and with
``ARGUS_MINT_READ_ONLY=1`` every write is refused. The server binds to localhost by default.
"""

from __future__ import annotations

import hashlib
import math
import os
import queue
import random
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import structlog
from argus_cortex.server import WriteGuard, constant_refuse, cross_site_refuse
from eth_utils import to_checksum_address
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

import argus_mint
from argus_mint import networks, siwe
from argus_mint._codec import canonical, hex0x, keccak256, to_bytes32
from argus_mint.bios import BioError, BioFailed, BioTemplate, ChatWriter, load_bio
from argus_mint.cards import (
    REVIEWERS,
    CardDraft,
    DraftError,
    DraftNotFound,
    DraftStore,
    review,
    sha256_hex,
    write_atomic,
)
from argus_mint.catalogue import CatalogueError, record_decision
from argus_mint.darkroom import DarkroomClient, DarkroomError
from argus_mint.frames import FrameError, LoadedFrame, compose, load_frame
from argus_mint.keys import KeyLoadError, load_signer
from argus_mint.moderation import WordFilter, auto_review
from argus_mint.pin import KuboPinner, PinError
from argus_mint.render import RenderError
from argus_mint.templates import Template, TemplateError, combinations, load_template
from argus_mint.wire import (
    BioInfo,
    Capabilities,
    CatalogueDecision,
    CatalogueEntry,
    Challenge,
    ChallengeRequest,
    CreateDraft,
    DraftDetail,
    DraftList,
    DraftSummary,
    EditTextRequest,
    FrameInfo,
    FramePreviewRequest,
    Health,
    PublishRequest,
    PublishResult,
    Ref,
    ReviewRequest,
    SampleItem,
    SampleJob,
    SampleJobList,
    SampleRequest,
    TemplateInfo,
    TextEdit,
)
from argus_mint.workflow import card_texts, check_bio, check_texts, create_draft, edit_text, publish, sample_card

__all__ = ["DEFAULT_PORT", "Settings", "create_app"]

log = structlog.get_logger(__name__)

DEFAULT_PORT = 8108
PREFIX = "ARGUS_MINT_"
LOCAL_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:3108", "http://127.0.0.1:3108")
READ_ONLY_REFUSAL = "argus-mint is read-only: drafts, reviews and publishing are disabled"
CHALLENGE_TTL = timedelta(minutes=10)
PROBE_TIMEOUT = 2.0


@dataclass(frozen=True)
class Settings:
    """Server configuration, normally from ``ARGUS_MINT_*`` environment variables."""

    drafts_dir: Path = Path("drafts")
    samples_dir: Path = Path("samples")
    templates_dir: Path = Path("templates")
    frames_dir: Path = Path("frames")
    bios_dir: Path = Path("bios")
    darkroom_url: str = ""
    policy: str = "default"
    writer_url: str = ""
    writer_key: str = ""
    ipfs_api: str = ""
    account: str = ""  # Foundry keystore; its password from ARGUS_MINT_KEYSTORE_PASSWORD_FILE (no prompt)
    network: str = "base-sepolia"
    card: str = ""
    consumer: str = "argus-mint"
    voucher_ttl: int = 3600
    moderator_token: str = ""
    siwe_domain: str = ""  # the UI's host[:port], as the owner's wallet shows it
    siwe_uri: str = ""  # default https://<siwe_domain>
    cors_origins: tuple[str, ...] = LOCAL_ORIGINS
    read_only: bool = False
    auto_moderate: bool = False  # the platform's checks stand in for the moderator when they all pass
    blocklist: Path | None = None  # extra word-filter terms, one per line

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)

        def get(name: str, default: str = "") -> str:
            return env.get(PREFIX + name, default).strip()

        origins = tuple(o.strip() for o in get("CORS_ORIGINS").split(",") if o.strip())
        return cls(
            drafts_dir=Path(get("DRAFTS_DIR", "drafts")),
            samples_dir=Path(get("SAMPLES_DIR", "samples")),
            templates_dir=Path(get("TEMPLATES_DIR", "templates")),
            frames_dir=Path(get("FRAMES_DIR", "frames")),
            bios_dir=Path(get("BIOS_DIR", "bios")),
            darkroom_url=get("DARKROOM_URL"),
            policy=get("POLICY", "default"),
            writer_url=get("WRITER_URL"),
            writer_key=get("WRITER_KEY"),
            ipfs_api=get("IPFS_API"),
            account=get("ACCOUNT"),
            network=get("NETWORK", "base-sepolia"),
            card=get("CARD"),
            consumer=get("CONSUMER", "argus-mint"),
            voucher_ttl=int(get("VOUCHER_TTL", "3600")),
            moderator_token=get("MODERATOR_TOKEN"),
            siwe_domain=get("SIWE_DOMAIN"),
            siwe_uri=get("SIWE_URI"),
            cors_origins=origins or LOCAL_ORIGINS,
            read_only=get("READ_ONLY").lower() in ("1", "true", "yes"),
            auto_moderate=get("AUTO_MODERATE").lower() in ("1", "true", "yes"),
            blocklist=Path(get("BLOCKLIST")) if get("BLOCKLIST") else None,
        )


def probe(url: str) -> bool:
    """Whether something answers at ``url``. Any HTTP answer counts (a 404 is a server that is up)."""
    if not url:
        return False
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=PROBE_TIMEOUT):
            pass
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return True


@dataclass
class _Challenges:
    """Owner challenges issued and not yet used: nonce -> (draft id, purpose, image sha256, expiry).

    The purpose is ``approve``, ``reject`` or ``edit:<sha256 of the canonical edit>``.
    """

    issued: dict[str, tuple[str, str, str, datetime]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, nonce: str, entry: tuple[str, str, str, datetime]) -> None:
        with self.lock:
            now = datetime.now(UTC)
            self.issued = {n: e for n, e in self.issued.items() if e[3] > now}
            self.issued[nonce] = entry

    def take(self, nonce: str) -> tuple[str, str, str, datetime] | None:
        with self.lock:
            return self.issued.pop(nonce, None)


def _statement(draft_id: str, purpose: str) -> str:
    if purpose.startswith("edit:"):
        return f"Change the card text of argus-mint draft {draft_id}."
    return (
        f"{'Approve' if purpose == 'approve' else 'Reject'} the card image of argus-mint draft {draft_id} for minting."
    )


def _resources(draft_id: str, image_sha256: str, purpose: str) -> tuple[str, ...]:
    out = (f"urn:argus-mint:draft:{draft_id}", f"urn:sha256:{image_sha256}")
    return (*out, f"urn:sha256:{purpose[5:]}") if purpose.startswith("edit:") else out


def _edit_hash(edit: TextEdit) -> str:
    """What an owner's signature on a text edit commits to: exactly these changes."""
    return hashlib.sha256(canonical(edit.model_dump(mode="json"))).hexdigest()


def create_app(
    settings: Settings | None = None,
    *,
    darkroom: Any = None,
    writer: Any = None,
    pinner: Any = None,
    signer: Any = None,
    probe_fn: Any = probe,
) -> FastAPI:
    """The app. Clients can be injected (tests); otherwise they are built from ``settings``."""
    cfg = settings or Settings.from_env()
    net = networks.get(cfg.network)
    store = DraftStore(cfg.drafts_dir)
    lock = threading.Lock()  # one writer at a time over the drafts directory
    challenges = _Challenges()
    darkroom = darkroom or (DarkroomClient(cfg.darkroom_url, policy=cfg.policy) if cfg.darkroom_url else None)
    writer = writer or (ChatWriter(cfg.writer_url, api_key=cfg.writer_key or None) if cfg.writer_url else None)
    pinner = pinner or (KuboPinner(cfg.ipfs_api) if cfg.ipfs_api else None)
    signer_box: dict[str, Any] = {"signer": signer}

    def get_signer() -> Any:
        if signer_box["signer"] is None:
            try:
                signer_box["signer"] = load_signer(cfg.account or None, prompt=_no_prompt)
            except KeyLoadError as e:
                raise HTTPException(503, f"no signer: {e}") from e
        return signer_box["signer"]

    words: dict[str, WordFilter] = {}

    def word_filter() -> WordFilter:
        if "filter" not in words:
            words["filter"] = WordFilter.from_file(cfg.blocklist)
        return words["filter"]

    if cfg.auto_moderate:
        word_filter()  # fail at start, not on the first draft, if the list can't load

    signer_configured = signer is not None or bool(cfg.account or os.environ.get("ARGUS_MINT_SIGNER_KEY"))
    writable = not cfg.read_only
    has_token = bool(cfg.moderator_token)

    def capabilities() -> Capabilities:
        return Capabilities(
            create_drafts=writable and has_token and darkroom is not None,
            write_bios=writable and has_token and darkroom is not None and writer is not None,
            moderate=writable and has_token,
            owner_review=writable and bool(cfg.siwe_domain),
            publish=writable and has_token and pinner is not None and signer_configured and bool(cfg.card),
            sample=writable and has_token and darkroom is not None,
            edit_text=writable and (has_token or bool(cfg.siwe_domain)),
            auto_moderate=cfg.auto_moderate,
            review_catalogue=writable
            and has_token
            and all(d.is_dir() and os.access(d, os.W_OK) for d in (cfg.templates_dir, cfg.frames_dir, cfg.bios_dir)),
        )

    app = FastAPI(title="argus-mint", version=argus_mint.__version__)
    named = [o for o in cfg.cors_origins if o != "*"]
    app.add_middleware(WriteGuard, refuse=cross_site_refuse(named))
    if cfg.read_only:
        app.add_middleware(WriteGuard, refuse=constant_refuse(READ_ONLY_REFUSAL))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if "*" in cfg.cors_origins else named,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # -- helpers ---------------------------------------------------------------------------------

    def require_token(authorization: str | None) -> None:
        if not has_token:
            raise HTTPException(403, "no moderator token is configured on this server")
        scheme, _, token = (authorization or "").partition(" ")
        given, expected = token.strip().encode(), cfg.moderator_token.encode()  # bytes: str refuses non-ASCII
        if scheme.lower() != "bearer" or not secrets.compare_digest(given, expected):
            raise HTTPException(401, "a valid moderator token is required", headers={"WWW-Authenticate": "Bearer"})

    def yaml_files(root: Path) -> list[Path]:
        return sorted([*root.rglob("*.yaml"), *root.rglob("*.yml")]) if root.is_dir() else []

    kinds: dict[str, tuple[Path, Any, type[Exception]]] = {
        "template": (cfg.templates_dir, load_template, TemplateError),
        "frame": (cfg.frames_dir, load_frame, FrameError),
        "bio": (cfg.bios_dir, load_bio, BioError),
    }

    def load_kind(kind: str) -> dict[tuple[str, int], tuple[Any, Path]]:
        """Every file of one kind (any status), keyed by (id, version), with its path."""
        root, loader, error = kinds[kind]
        out: dict[tuple[str, int], tuple[Any, Path]] = {}
        for f in yaml_files(root):
            try:
                obj = loader(f)
            except error as e:
                raise HTTPException(500, f"{kind} directory is invalid: {e}") from e
            meta = obj.frame if kind == "frame" else obj
            key = (meta.id, meta.version)
            if key in out:
                raise HTTPException(500, f"duplicate {kind} {key[0]} v{key[1]}: {out[key][1]} and {f}")
            out[key] = (obj, f)
        return out

    def templates() -> dict[tuple[str, int], Template]:
        return {k: v[0] for k, v in load_kind("template").items()}

    def frames() -> dict[tuple[str, int], LoadedFrame]:
        return {k: v[0] for k, v in load_kind("frame").items()}

    def bios() -> dict[tuple[str, int], BioTemplate]:
        return {k: v[0] for k, v in load_kind("bio").items()}

    def rel(kind: str, path: Path) -> str:
        return str(path.relative_to(kinds[kind][0]))

    def pick(table: dict[tuple[str, int], Any], ref: Ref, kind: str) -> Any:
        try:
            return table[(ref.id, ref.version)]
        except KeyError:
            raise HTTPException(404, f"no {kind} {ref.id} v{ref.version}") from None

    def status_of(draft_id: str, d: CardDraft) -> str:
        return "published" if store.published(draft_id) else d.status

    def detail(draft_id: str) -> DraftDetail:
        try:
            d, _art, _image = store.load(draft_id)
        except DraftNotFound as e:
            raise HTTPException(404, str(e)) from e
        except DraftError as e:
            raise HTTPException(400 if "not a draft id" in str(e) else 500, str(e)) from e
        pub = store.published(draft_id)
        return DraftDetail(
            id=draft_id,
            status=status_of(draft_id, d),  # type: ignore[arg-type]
            awaiting=d.awaiting(),
            draft=d,
            published=PublishResult.model_validate(pub) if pub else None,
        )

    # -- reads -----------------------------------------------------------------------------------

    @app.get("/health")
    def health() -> Health:
        return Health(
            version=argus_mint.__version__,
            read_only=cfg.read_only,
            network=net.name,
            chain_id=net.chain_id,
            card=cfg.card or None,
            consumer=cfg.consumer,
            reviewers=list(REVIEWERS),
            siwe_domain=cfg.siwe_domain or None,
            capabilities=capabilities(),
            backends={
                "darkroom": probe_fn(f"{cfg.darkroom_url.rstrip('/')}/health") if cfg.darkroom_url else False,
                "writer": probe_fn(f"{cfg.writer_url.rstrip('/')}/health") if cfg.writer_url else False,
                "ipfs": probe_fn(f"{cfg.ipfs_api.rstrip('/')}/api/v0/version") if cfg.ipfs_api else False,
            },
        )

    @app.get("/templates")
    def list_templates() -> list[TemplateInfo]:
        return [
            TemplateInfo(
                hash=hex0x(t.hash()),
                file=rel("template", path),
                combinations=math.prod(len(v) for v in t.variables.values()),
                template=t,
            )
            for _k, (t, path) in sorted(load_kind("template").items())
        ]

    @app.get("/frames")
    def list_frames() -> list[FrameInfo]:
        return [
            FrameInfo(hash=hex0x(f.hash()), file=rel("frame", path), width=f.size[0], height=f.size[1], frame=f.frame)
            for _k, (f, path) in sorted(load_kind("frame").items())
        ]

    @app.get("/frames/{frame_id}/{version}/overlay")
    def frame_overlay(frame_id: str, version: int) -> Response:
        f = pick(frames(), Ref(id=frame_id, version=version), "frame")
        return Response(f.overlay, media_type="image/png")

    @app.get("/bios")
    def list_bios() -> list[BioInfo]:
        return [
            BioInfo(hash=hex0x(b.hash()), file=rel("bio", path), bio=b)
            for _k, (b, path) in sorted(load_kind("bio").items())
        ]

    @app.get("/drafts")
    def list_drafts(
        status: Annotated[str | None, Query(pattern="^(pending|approved|rejected|published)$")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> DraftList:
        items = []
        for draft_id in store.ids():
            try:
                d, _a, _i = store.load(draft_id)
            except DraftError:
                continue
            s = status_of(draft_id, d)
            if status and s != status:
                continue
            items.append(
                DraftSummary(
                    id=draft_id,
                    name=d.name,
                    to=d.to,
                    status=s,  # type: ignore[arg-type]
                    awaiting=d.awaiting(),
                    created_at=d.created_at,
                    template=Ref(id=d.request.template_id, version=d.request.template_version),
                    frame=Ref(id=d.frame.id, version=d.frame.version) if d.frame else None,
                    image_sha256=d.image_sha256,
                )
            )
        return DraftList(items=items[offset : offset + limit], total=len(items))

    @app.get("/drafts/{draft_id}")
    def get_draft(draft_id: str) -> DraftDetail:
        return detail(draft_id)

    def png(draft_id: str, which: str) -> Response:
        try:
            _d, art, image = store.load(draft_id)
        except DraftNotFound as e:
            raise HTTPException(404, str(e)) from e
        except DraftError as e:
            raise HTTPException(400, str(e)) from e
        return Response(image if which == "image" else art, media_type="image/png")

    @app.get("/drafts/{draft_id}/image")
    def draft_image(draft_id: str) -> Response:
        return png(draft_id, "image")

    @app.get("/drafts/{draft_id}/art")
    def draft_art(draft_id: str) -> Response:
        return png(draft_id, "art")

    # -- writes ----------------------------------------------------------------------------------

    # The handlers that render, composite, pin or take the lock are plain `def`s: FastAPI runs
    # those in its threadpool, so none of that work (or a wait for the lock) stalls the event loop.

    @app.post("/drafts", status_code=201)
    def post_draft(body: CreateDraft, authorization: Annotated[str | None, Header()] = None) -> DraftDetail:
        require_token(authorization)
        if darkroom is None:
            raise HTTPException(503, "no darkroom is configured (ARGUS_MINT_DARKROOM_URL)")
        if (body.asset is None) == (body.asset_id is None):
            raise HTTPException(422, "give exactly one of asset or asset_id")
        try:
            aid = to_bytes32(body.asset_id) if body.asset_id is not None else keccak256(body.asset.encode())  # type: ignore[union-attr]
            to = to_checksum_address(body.to)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        t = pick(templates(), body.template, "template")
        lf = pick(frames(), body.frame, "frame") if body.frame else None
        bt = pick(bios(), body.bio, "bio template") if body.bio else None
        if bt is not None:
            if writer is None:
                raise HTTPException(503, "no bio writer is configured (ARGUS_MINT_WRITER_URL)")
            try:
                check_bio(bt, body.variables, t)  # the request's fault: a BioError past here is the writer's
            except BioError as e:
                raise HTTPException(422, str(e)) from e
        # BioError (and so BioFailed) is a ValueError: the specific handlers come before the 422 catch-all.
        try:
            made = create_draft(
                template=t, values=body.variables, seed=body.seed, asset_id=aid, to=to, name=body.name,
                renderer=darkroom, description=body.description, external_url=body.external_url,
                consumer=cfg.consumer, frame=lf, bio=bt, writer=writer if bt else None,
                guard=darkroom if bt else None, texts=body.texts,
            )  # fmt: skip
        except RenderError as e:
            raise HTTPException(
                422, {"error": str(e), "attempts": [a.__dict__ | {"outcome": str(a.outcome)} for a in e.attempts]}
            ) from e
        except BioFailed as e:
            raise HTTPException(422, {"error": str(e), "bio_attempts": [a.__dict__ for a in e.attempts]}) from e
        except (DarkroomError, BioError) as e:
            raise HTTPException(502, str(e)) from e
        except (TemplateError, FrameError, ValueError) as e:
            raise HTTPException(422, str(e)) from e
        with lock:
            draft_id = store.new_id()
            store.save(draft_id, moderate_automatically(made.draft, made.image), made.art, made.image)
        return detail(draft_id)

    @app.post("/drafts/{draft_id}/challenge")
    def post_challenge(draft_id: str, body: ChallengeRequest) -> Challenge:
        if not cfg.siwe_domain:
            raise HTTPException(503, "owner review is off: no SIWE domain is configured (ARGUS_MINT_SIWE_DOMAIN)")
        d = detail(draft_id)
        if body.action == "edit":
            if body.edit is None:
                raise HTTPException(422, "an edit challenge needs the edit it is for")
            if d.status == "published":
                raise HTTPException(409, "a published card can't be edited")
            purpose = f"edit:{_edit_hash(body.edit)}"
        else:
            if d.status != "pending" or "owner" not in d.awaiting:
                raise HTTPException(409, f"the owner can't review this draft now (status {d.status})")
            purpose = "approve" if body.approve else "reject"
        now = datetime.now(UTC)
        nonce = siwe.make_nonce()
        msg = siwe.create_message(
            domain=cfg.siwe_domain,
            address=d.draft.to,
            uri=cfg.siwe_uri or f"https://{cfg.siwe_domain}",
            chain_id=net.chain_id,
            nonce=nonce,
            issued_at=now,
            ttl=CHALLENGE_TTL,
            statement=_statement(draft_id, purpose),
            resources=_resources(draft_id, d.draft.image_sha256, purpose),
        )
        challenges.add(nonce, (draft_id, purpose, d.draft.image_sha256, now + CHALLENGE_TTL))
        return Challenge(message=msg.text(), nonce=nonce, expires_at=now + CHALLENGE_TTL)

    def moderate_automatically(d: CardDraft, image: bytes) -> CardDraft:
        return auto_review(d, image, word_filter()) if cfg.auto_moderate else d

    @app.post("/drafts/{draft_id}/text")
    def post_text(
        draft_id: str, body: EditTextRequest, authorization: Annotated[str | None, Header()] = None
    ) -> DraftDetail:
        edit = TextEdit(name=body.name, bio=body.bio, texts=body.texts)
        if body.role == "moderator":
            require_token(authorization)
            if not body.by:
                raise HTTPException(422, "the moderator's edit needs `by` (their name or id)")
            by = body.by
        else:
            by = _verify_owner(draft_id, body.message, body.signature, f"edit:{_edit_hash(edit)}")
        with lock:
            try:
                d, art, _image = store.load(draft_id)
                if store.published(draft_id):
                    raise DraftError("a published card can't be edited")
                if d.frame is None:
                    raise DraftError("only a framed card has text to edit")
                lf = frames().get((d.frame.id, d.frame.version))
                if lf is None:
                    raise DraftError(f"frame {d.frame.id} v{d.frame.version} is no longer served here")
                d, image = edit_text(d, art, lf, role=body.role, by=by, name=edit.name, bio=edit.bio, texts=edit.texts)
                d = moderate_automatically(d, image)
                store.save(draft_id, d, image=image)
            except DraftNotFound as e:
                raise HTTPException(404, str(e)) from e
            except (DraftError, FrameError) as e:
                raise HTTPException(409, str(e)) from e
        return detail(draft_id)

    @app.post("/drafts/{draft_id}/review")
    def post_review(
        draft_id: str, body: ReviewRequest, authorization: Annotated[str | None, Header()] = None
    ) -> DraftDetail:
        if body.role == "moderator":
            require_token(authorization)
            if not body.by:
                raise HTTPException(422, "the moderator's review needs `by` (their name or id)")
            by = body.by
        else:
            by = _verify_owner(draft_id, body.message, body.signature, "approve" if body.approve else "reject")
        with lock:
            try:
                d, _art, image = store.load(draft_id)
                if store.published(draft_id):
                    raise DraftError("this draft is already published")
                if sha256_hex(image) != d.image_sha256:
                    raise DraftError("the card image on disk differs from the one rendered for this draft")
                d = review(d, role=body.role, approve=body.approve, by=by, note=body.note)
                store.save(draft_id, d)
            except DraftNotFound as e:
                raise HTTPException(404, str(e)) from e
            except DraftError as e:
                raise HTTPException(409, str(e)) from e
        return detail(draft_id)

    def _verify_owner(draft_id: str, message: str | None, signature: str | None, purpose: str) -> str:
        """Check a signed owner challenge for ``purpose`` on this draft's current image; return the address."""
        if not cfg.siwe_domain:
            raise HTTPException(503, "owner actions are off: no SIWE domain is configured")
        if not message or not signature:
            raise HTTPException(422, "the owner needs the signed challenge: message and signature")
        try:
            parsed = siwe.parse(message)
        except siwe.SiweError as e:
            raise HTTPException(401, f"not a valid sign-in message: {e}") from e
        entry = challenges.take(parsed.nonce)
        if entry is None:
            raise HTTPException(401, "unknown or already used challenge; ask for a new one")
        issued_for, issued_purpose, image_sha, _exp = entry
        d = detail(draft_id).draft
        if issued_for != draft_id or issued_purpose != purpose or image_sha != d.image_sha256:
            raise HTTPException(401, "the challenge was issued for another draft, action or image")
        try:
            sig = bytes.fromhex(signature.removeprefix("0x"))
            msg = siwe.verify(
                message, sig, domain=cfg.siwe_domain, nonce=parsed.nonce, now=datetime.now(UTC),
                chain_id=net.chain_id,
            )  # fmt: skip
        except (ValueError, siwe.SiweError) as e:
            raise HTTPException(401, f"signature check failed: {e}") from e
        if msg.statement != _statement(draft_id, purpose) or msg.resources != _resources(
            draft_id, d.image_sha256, purpose
        ):
            raise HTTPException(401, "the signed message is not the challenge that was issued")
        if to_checksum_address(msg.address) != to_checksum_address(d.to):
            raise HTTPException(403, "only the wallet the card is minted to can act as the owner")
        return msg.address

    @app.post("/drafts/{draft_id}/publish")
    def post_publish(
        draft_id: str, body: PublishRequest, authorization: Annotated[str | None, Header()] = None
    ) -> DraftDetail:
        require_token(authorization)
        if pinner is None or not cfg.card:
            raise HTTPException(503, "publishing needs ARGUS_MINT_IPFS_API and ARGUS_MINT_CARD")
        if body.deadline is not None and body.deadline <= time.time():
            # a draft publishes once: an already expired voucher would close it for good
            raise HTTPException(422, "deadline is in the past: the voucher would already be expired")
        signer_now = get_signer()
        if signer_now is None:
            raise HTTPException(503, "no signer: set ARGUS_MINT_ACCOUNT (+ password file) or ARGUS_MINT_SIGNER_KEY")
        try:
            with lock:
                if store.published(draft_id):
                    raise DraftError("this draft is already published")
                d, art, image = store.load(draft_id)
                if d.renderer == "placeholder":
                    raise DraftError("this draft has placeholder art and can't be published")
                result = publish(
                    d, art, image, pinner=pinner, signer=signer_now, chain_id=net.chain_id, card=cfg.card,
                    price=body.price, deadline=body.deadline, ttl=cfg.voucher_ttl,
                )  # fmt: skip
                store.save_published(draft_id, result)
        except DraftNotFound as e:
            raise HTTPException(404, str(e)) from e
        except DraftError as e:  # NotApproved included
            raise HTTPException(409, str(e)) from e
        except PinError as e:
            raise HTTPException(502, str(e)) from e
        return detail(draft_id)

    # -- reviewing the catalogue: samples, frame previews, approve / retire -----------------------

    samples = _SampleRunner(cfg.samples_dir)

    def catalogue_entry(kind: str, obj: Any, path: Path) -> CatalogueEntry:
        meta = obj.frame if kind == "frame" else obj
        return CatalogueEntry(
            kind=kind,  # type: ignore[arg-type]
            id=meta.id,
            version=meta.version,
            status=meta.status,
            hash=hex0x(obj.hash()),
            file=rel(kind, path),
        )

    def decision_handler(kind: str) -> Any:
        def decide(
            item_id: str, version: int, body: CatalogueDecision, authorization: Annotated[str | None, Header()] = None
        ) -> CatalogueEntry:
            require_token(authorization)
            table = load_kind(kind)
            if (item_id, version) not in table:
                raise HTTPException(404, f"no {kind} {item_id} v{version}")
            _obj, path = table[(item_id, version)]
            if not os.access(path, os.W_OK):
                raise HTTPException(503, f"{rel(kind, path)} is not writable on this server")
            loader = kinds[kind][1]
            with lock:
                try:
                    record_decision(path, body.decision, by=body.by, today=datetime.now(UTC).date(), validate=loader)
                except CatalogueError as e:
                    raise HTTPException(409, str(e)) from e
            return catalogue_entry(kind, loader(path), path)

        return decide

    for kind, prefix in (("template", "templates"), ("frame", "frames"), ("bio", "bios")):
        app.post(f"/{prefix}/{{item_id}}/{{version}}/review", name=f"review_{kind}")(decision_handler(kind))

    @app.post("/samples", status_code=202)
    def post_samples(body: SampleRequest, authorization: Annotated[str | None, Header()] = None) -> SampleJob:
        require_token(authorization)
        if darkroom is None:
            raise HTTPException(503, "no darkroom is configured (ARGUS_MINT_DARKROOM_URL)")
        t = pick(templates(), body.template, "template")
        lf = pick(frames(), body.frame, "frame") if body.frame else None
        bt = pick(bios(), body.bio, "bio template") if body.bio else None
        if bt is not None and writer is None:
            raise HTTPException(503, "no bio writer is configured (ARGUS_MINT_WRITER_URL)")
        try:
            check_texts(body.texts, lf)
        except FrameError as e:
            raise HTTPException(422, str(e)) from e
        unknown = sorted(body.variables.keys() - t.variables.keys())
        bad = sorted(k for k, v in body.variables.items() if k in t.variables and v not in t.variables[k])
        if unknown or bad:
            raise HTTPException(422, f"unknown variables {unknown}, or values not in the template's lists {bad}")
        pool = [c for c in combinations(t) if all(c[k] == v for k, v in body.variables.items())]
        picks = random.Random(body.seed).sample(pool, min(body.count, len(pool)))

        def run_one(n: int, values: dict[str, str]) -> Any:
            return sample_card(
                renderer=darkroom, template=t, values=values, seed=(body.seed + n) % (2**32), frame=lf, bio=bt,
                writer=writer if bt else None, guard=darkroom if bt else None, name=body.name, texts=body.texts,
            )  # fmt: skip

        return samples.submit(body, picks, run_one)

    @app.get("/samples")
    def list_samples(
        limit: Annotated[int, Query(ge=1, le=200)] = 50, offset: Annotated[int, Query(ge=0)] = 0
    ) -> SampleJobList:
        jobs = samples.all()
        return SampleJobList(items=jobs[offset : offset + limit], total=len(jobs))

    @app.get("/samples/{job_id}")
    def get_sample(job_id: str) -> SampleJob:
        return samples.get(job_id)

    @app.get("/samples/{job_id}/{n}/{which}")
    def sample_image(job_id: str, n: int, which: str) -> Response:
        if which not in ("art", "framed"):
            raise HTTPException(404, "art or framed")
        return Response(samples.image(job_id, n, which), media_type="image/png")

    @app.post("/frames/{frame_id}/{version}/preview")
    def frame_preview(frame_id: str, version: int, body: FramePreviewRequest) -> Response:
        lf = pick(frames(), Ref(id=frame_id, version=version), "frame")
        src = body.art
        if (src.draft is None) == (src.sample is None):
            raise HTTPException(422, "give art.draft, or art.sample with art.n")
        if src.draft is not None:
            try:
                _d, art, _image = store.load(src.draft)
            except DraftNotFound as e:
                raise HTTPException(404, str(e)) from e
            except DraftError as e:
                raise HTTPException(400, str(e)) from e
        else:
            art = samples.image(src.sample or "", src.n or 0, "art")
        try:
            check_texts(body.texts, lf)
            png = compose(lf, art, card_texts(lf, body.name, body.bio, body.texts))
        except FrameError as e:
            raise HTTPException(422, str(e)) from e
        return Response(png, media_type="image/png")

    return app


def _no_prompt(_prompt: str) -> str:
    raise KeyLoadError("the server can't prompt for a keystore password: set ARGUS_MINT_KEYSTORE_PASSWORD_FILE")


class _SampleRunner:
    """Sample jobs, run one at a time on a background thread (darkroom renders one at a time).

    Each job is a directory under ``root``: ``job.json`` plus ``<n>-art.png`` and
    ``<n>-framed.png``. Nothing here is pinned, signed or minted. A job that was queued or
    running when the server stopped is marked failed when next listed.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.queue: queue.Queue[tuple[str, list[dict[str, str]], Any]] = queue.Queue()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.live: set[str] = set()

    def _dir(self, job_id: str) -> Path:
        if not _JOB_ID.match(job_id):
            raise HTTPException(400, f"not a sample job id: {job_id!r}")
        return self.root / job_id

    def _save(self, job: SampleJob) -> None:
        d = self._dir(job.id)
        d.mkdir(parents=True, exist_ok=True)
        write_atomic(d / "job.json", job.model_dump_json(indent=2) + "\n")  # GET /samples reads it mid-job

    def get(self, job_id: str) -> SampleJob:
        f = self._dir(job_id) / "job.json"
        if not f.is_file():
            raise HTTPException(404, f"no sample job {job_id}")
        # Ask before reading: a job that finishes between the two (its last save, then leaving
        # `live`) would otherwise be read as running, found not live, and reported as interrupted.
        live = job_id in self.live
        job = SampleJob.model_validate_json(f.read_text(encoding="utf-8"))
        if job.status in ("queued", "running") and not live:
            job = job.model_copy(update={"status": "failed", "error": "interrupted: the server stopped"})
        return job

    def all(self) -> list[SampleJob]:
        if not self.root.is_dir():
            return []
        found = [p for p in self.root.iterdir() if _JOB_ID.match(p.name) and (p / "job.json").is_file()]
        ordered = sorted(found, key=lambda p: (p / "job.json").stat().st_mtime, reverse=True)
        return [self.get(p.name) for p in ordered]

    def image(self, job_id: str, n: int, which: str) -> bytes:
        f = self._dir(job_id) / f"{int(n)}-{which}.png"
        if not f.is_file():
            raise HTTPException(404, f"sample {job_id} #{n} has no {which} image")
        return f.read_bytes()

    def submit(self, request: SampleRequest, picks: list[dict[str, str]], run_one: Any) -> SampleJob:
        job = SampleJob(id=secrets.token_hex(8), status="queued", created_at=datetime.now(UTC), request=request)
        with self.lock:
            self.live.add(job.id)
            self._save(job)
            self.queue.put((job.id, picks, run_one))
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._work, name="argus-mint-samples", daemon=True)
                self.thread.start()
        return job

    def _work(self) -> None:
        while True:
            try:
                job_id, picks, run_one = self.queue.get(timeout=5)
            except queue.Empty:
                # Retire under the lock submit() holds: a job queued while this thread was on its
                # way out would otherwise find it "alive" and never be picked up.
                with self.lock:
                    if self.queue.empty():
                        self.thread = None
                        return
                continue
            try:
                self._run(job_id, picks, run_one)
            except Exception as e:  # e.g. job.json can't be written: keep working through the queue
                log.warning("sample_job_crashed", job=job_id, error=str(e))
            finally:
                self.live.discard(job_id)

    def _run(self, job_id: str, picks: list[dict[str, str]], run_one: Any) -> None:
        job = self.get(job_id).model_copy(update={"status": "running"})
        self._save(job)
        items: list[SampleItem] = []
        try:
            for n, values in enumerate(picks, 1):
                sample = run_one(n, values)
                d = self._dir(job_id)
                if sample.art is not None:
                    (d / f"{n}-art.png").write_bytes(sample.art)
                if sample.framed is not None:
                    (d / f"{n}-framed.png").write_bytes(sample.framed)
                items.append(
                    SampleItem(n=n, art=sample.art is not None, framed=sample.framed is not None, **sample.item)
                )
                job = job.model_copy(update={"items": list(items)})
                self._save(job)
        except Exception as e:  # a backend failure ends the job; it is reported, not raised
            log.warning("sample_job_failed", job=job_id, error=str(e))
            self._save(job.model_copy(update={"status": "failed", "error": str(e), "finished_at": datetime.now(UTC)}))
            return
        self._save(job.model_copy(update={"status": "done", "finished_at": datetime.now(UTC)}))


_JOB_ID = re.compile(r"^[0-9a-f]{16}$")
