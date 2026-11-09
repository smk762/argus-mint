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

import os
import secrets
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from argus_cortex.server import WriteGuard, constant_refuse, cross_site_refuse
from eth_utils import to_checksum_address
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

import argus_mint
from argus_mint import networks, siwe
from argus_mint._codec import hex0x, keccak256, to_bytes32
from argus_mint.bios import BioError, BioFailed, BioTemplate, ChatWriter, load_bio
from argus_mint.cards import (
    REVIEWERS,
    CardDraft,
    DraftError,
    DraftNotFound,
    DraftStore,
    NotApproved,
    review,
    sha256_hex,
)
from argus_mint.darkroom import DarkroomClient, DarkroomError
from argus_mint.frames import FrameError, LoadedFrame, load_frame
from argus_mint.keys import KeyLoadError, load_signer
from argus_mint.pin import KuboPinner, PinError
from argus_mint.render import RenderError
from argus_mint.templates import Template, TemplateError, load_registry
from argus_mint.wire import (
    BioInfo,
    Capabilities,
    Challenge,
    ChallengeRequest,
    CreateDraft,
    DraftDetail,
    DraftList,
    DraftSummary,
    FrameInfo,
    Health,
    PublishRequest,
    PublishResult,
    Ref,
    ReviewRequest,
    TemplateInfo,
)
from argus_mint.workflow import create_draft, publish

__all__ = ["DEFAULT_PORT", "Settings", "create_app"]

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

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)

        def get(name: str, default: str = "") -> str:
            return env.get(PREFIX + name, default).strip()

        origins = tuple(o.strip() for o in get("CORS_ORIGINS").split(",") if o.strip())
        return cls(
            drafts_dir=Path(get("DRAFTS_DIR", "drafts")),
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
    """Owner challenges issued and not yet used: nonce -> (draft id, approve, image sha256, expiry)."""

    issued: dict[str, tuple[str, bool, str, datetime]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, nonce: str, entry: tuple[str, bool, str, datetime]) -> None:
        with self.lock:
            now = datetime.now(UTC)
            self.issued = {n: e for n, e in self.issued.items() if e[3] > now}
            self.issued[nonce] = entry

    def take(self, nonce: str) -> tuple[str, bool, str, datetime] | None:
        with self.lock:
            return self.issued.pop(nonce, None)


def _statement(draft_id: str, approve: bool) -> str:
    return f"{'Approve' if approve else 'Reject'} the card image of argus-mint draft {draft_id} for minting."


def _resources(draft_id: str, image_sha256: str) -> tuple[str, ...]:
    return (f"urn:argus-mint:draft:{draft_id}", f"urn:sha256:{image_sha256}")


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
        if scheme.lower() != "bearer" or not secrets.compare_digest(token.strip(), cfg.moderator_token):
            raise HTTPException(401, "a valid moderator token is required", headers={"WWW-Authenticate": "Bearer"})

    def templates() -> dict[tuple[str, int], Template]:
        try:
            return load_registry(cfg.templates_dir) if cfg.templates_dir.is_dir() else {}
        except TemplateError as e:
            raise HTTPException(500, f"template directory is invalid: {e}") from e

    def yaml_files(root: Path) -> list[Path]:
        return sorted([*root.rglob("*.yaml"), *root.rglob("*.yml")]) if root.is_dir() else []

    def frames() -> dict[tuple[str, int], LoadedFrame]:
        out = {}
        for f in yaml_files(cfg.frames_dir):
            try:
                lf = load_frame(f)
            except FrameError as e:
                raise HTTPException(500, f"frame directory is invalid: {e}") from e
            out[(lf.frame.id, lf.frame.version)] = lf
        return out

    def bios() -> dict[tuple[str, int], BioTemplate]:
        out = {}
        for f in yaml_files(cfg.bios_dir):
            try:
                bt = load_bio(f)
            except BioError as e:
                raise HTTPException(500, f"bio directory is invalid: {e}") from e
            out[(bt.id, bt.version)] = bt
        return out

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
        return [TemplateInfo(hash=hex0x(t.hash()), template=t) for _k, t in sorted(templates().items())]

    @app.get("/frames")
    def list_frames() -> list[FrameInfo]:
        return [
            FrameInfo(hash=hex0x(f.hash()), width=f.size[0], height=f.size[1], frame=f.frame)
            for _k, f in sorted(frames().items())
        ]

    @app.get("/frames/{frame_id}/{version}/overlay")
    def frame_overlay(frame_id: str, version: int) -> Response:
        f = pick(frames(), Ref(id=frame_id, version=version), "frame")
        return Response(f.overlay, media_type="image/png")

    @app.get("/bios")
    def list_bios() -> list[BioInfo]:
        return [BioInfo(hash=hex0x(b.hash()), bio=b) for _k, b in sorted(bios().items())]

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

    @app.post("/drafts", status_code=201)
    async def post_draft(body: CreateDraft, authorization: Annotated[str | None, Header()] = None) -> DraftDetail:
        require_token(authorization)
        if darkroom is None:
            raise HTTPException(503, "no darkroom is configured (ARGUS_MINT_DARKROOM_URL)")
        if (body.asset is None) == (body.asset_id is None):
            raise HTTPException(422, "give exactly one of asset or asset_id")
        try:
            aid = to_bytes32(body.asset_id) if body.asset_id else keccak256(body.asset.encode())  # type: ignore[union-attr]
            to = to_checksum_address(body.to)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        t = pick(templates(), body.template, "template")
        lf = pick(frames(), body.frame, "frame") if body.frame else None
        bt = pick(bios(), body.bio, "bio template") if body.bio else None
        if bt is not None and writer is None:
            raise HTTPException(503, "no bio writer is configured (ARGUS_MINT_WRITER_URL)")

        def make() -> Any:
            return create_draft(
                template=t, values=body.variables, seed=body.seed, asset_id=aid, to=to, name=body.name,
                renderer=darkroom, description=body.description, external_url=body.external_url,
                consumer=cfg.consumer, frame=lf, bio=bt, writer=writer if bt else None,
                guard=darkroom if bt else None, texts=body.texts,
            )  # fmt: skip

        try:
            made = await run_in_threadpool(make)
        except (TemplateError, FrameError, ValueError) as e:
            raise HTTPException(422, str(e)) from e
        except RenderError as e:
            raise HTTPException(
                422, {"error": str(e), "attempts": [a.__dict__ | {"outcome": str(a.outcome)} for a in e.attempts]}
            ) from e
        except BioFailed as e:
            raise HTTPException(422, {"error": str(e), "bio_attempts": [a.__dict__ for a in e.attempts]}) from e
        except (DarkroomError, BioError) as e:
            raise HTTPException(502, str(e)) from e
        with lock:
            draft_id = store.new_id()
            store.save(draft_id, made.draft, made.art, made.image)
        return detail(draft_id)

    @app.post("/drafts/{draft_id}/challenge")
    def post_challenge(draft_id: str, body: ChallengeRequest) -> Challenge:
        if not cfg.siwe_domain:
            raise HTTPException(503, "owner review is off: no SIWE domain is configured (ARGUS_MINT_SIWE_DOMAIN)")
        d = detail(draft_id)
        if d.status != "pending" or "owner" not in d.awaiting:
            raise HTTPException(409, f"the owner can't review this draft now (status {d.status})")
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
            statement=_statement(draft_id, body.approve),
            resources=_resources(draft_id, d.draft.image_sha256),
        )
        challenges.add(nonce, (draft_id, body.approve, d.draft.image_sha256, now + CHALLENGE_TTL))
        return Challenge(message=msg.text(), nonce=nonce, expires_at=now + CHALLENGE_TTL)

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
            by = _verify_owner(draft_id, body)
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

    def _verify_owner(draft_id: str, body: ReviewRequest) -> str:
        if not cfg.siwe_domain:
            raise HTTPException(503, "owner review is off: no SIWE domain is configured")
        if not body.message or not body.signature:
            raise HTTPException(422, "the owner's review needs the signed challenge: message and signature")
        try:
            parsed = siwe.parse(body.message)
        except siwe.SiweError as e:
            raise HTTPException(401, f"not a valid sign-in message: {e}") from e
        entry = challenges.take(parsed.nonce)
        if entry is None:
            raise HTTPException(401, "unknown or already used challenge; ask for a new one")
        issued_for, approve, image_sha, _exp = entry
        d = detail(draft_id).draft
        if issued_for != draft_id or approve != body.approve or image_sha != d.image_sha256:
            raise HTTPException(401, "the challenge was issued for another draft, decision or image")
        try:
            sig = bytes.fromhex(body.signature.removeprefix("0x"))
            msg = siwe.verify(
                body.message, sig, domain=cfg.siwe_domain, nonce=parsed.nonce, now=datetime.now(UTC),
                chain_id=net.chain_id,
            )  # fmt: skip
        except (ValueError, siwe.SiweError) as e:
            raise HTTPException(401, f"signature check failed: {e}") from e
        if msg.statement != _statement(draft_id, approve) or msg.resources != _resources(draft_id, d.image_sha256):
            raise HTTPException(401, "the signed message is not the challenge that was issued")
        if to_checksum_address(msg.address) != to_checksum_address(d.to):
            raise HTTPException(403, "only the wallet the card is minted to can review as the owner")
        return msg.address

    @app.post("/drafts/{draft_id}/publish")
    async def post_publish(
        draft_id: str, body: PublishRequest, authorization: Annotated[str | None, Header()] = None
    ) -> DraftDetail:
        require_token(authorization)
        if pinner is None or not cfg.card:
            raise HTTPException(503, "publishing needs ARGUS_MINT_IPFS_API and ARGUS_MINT_CARD")
        signer_now = get_signer()
        if signer_now is None:
            raise HTTPException(503, "no signer: set ARGUS_MINT_ACCOUNT (+ password file) or ARGUS_MINT_SIGNER_KEY")

        def run() -> dict[str, Any]:
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
                return result

        try:
            await run_in_threadpool(run)
        except DraftNotFound as e:
            raise HTTPException(404, str(e)) from e
        except NotApproved as e:
            raise HTTPException(409, str(e)) from e
        except DraftError as e:
            raise HTTPException(409, str(e)) from e
        except PinError as e:
            raise HTTPException(502, str(e)) from e
        return detail(draft_id)

    return app


def _no_prompt(_prompt: str) -> str:
    raise KeyLoadError("the server can't prompt for a keystore password: set ARGUS_MINT_KEYSTORE_PASSWORD_FILE")
