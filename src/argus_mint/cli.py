"""Command-line interface (the ``cli`` extra): ``argus-mint`` or ``python -m argus_mint``."""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path
from typing import Annotated, Any

import structlog
import typer
from eth_account import Account

import argus_mint
from argus_mint import merkle, networks
from argus_mint._codec import canonical, hex0x, keccak256, to_bytes32
from argus_mint.bios import WRITER_KEY_ENV, BioError, BioFailed, ChatWriter, load_bio, write_bio
from argus_mint.cards import (
    ART_FILE,
    CARD_FILE,
    DRAFT_FILE,
    REVIEWERS,
    DraftError,
    check_publishable,
    load_draft,
    review,
    save_draft,
    sha256_hex,
)
from argus_mint.darkroom import DarkroomClient, DarkroomError
from argus_mint.frames import FrameError, compose, load_frame
from argus_mint.keys import KEY_ENV, KeyLoadError, load_signer
from argus_mint.pin import KuboPinner, LocalPinner, PinError, Pinner
from argus_mint.render import GenResult, RenderError, render_for_review
from argus_mint.templates import (
    RenderRequest,
    TemplateError,
    combinations,
    fill_for_review,
    load_registry,
    load_template,
)
from argus_mint.vouchers import Domain, MintVoucher, recover
from argus_mint.workflow import bio_attempts, card_texts, create_draft
from argus_mint.workflow import publish as publish_draft

app = typer.Typer(help="argus-mint: provenance tokens for game assets.", no_args_is_help=True)

DarkroomOpt = Annotated[
    str | None,
    typer.Option(envvar="ARGUS_MINT_DARKROOM_URL", help="argus-darkroom base URL, e.g. http://localhost:8106"),
]
PolicyOpt = Annotated[str, typer.Option(help="darkroom enforcing policy (default: the SFW policy)")]
AccountOpt = Annotated[
    str | None,
    typer.Option(
        envvar="ARGUS_MINT_ACCOUNT",
        help="Foundry keystore account (name in ~/.foundry/keystores, or a path); "
        f"password from $ARGUS_MINT_KEYSTORE_PASSWORD_FILE or a prompt. Default: ${KEY_ENV}",
    ),
]
NetworkOpt = Annotated[str, typer.Option(help="base | base-sepolia | anvil")]
RpcOpt = Annotated[
    str | None,
    typer.Option(envvar="ARGUS_MINT_RPC_URL", help="JSON-RPC URL (default: the network's public endpoints)"),
]


class _Stderr:
    """Resolves sys.stderr at write time (it may be swapped, e.g. by test runners)."""

    def write(self, s: str) -> int:
        return sys.stderr.write(s)

    def flush(self) -> None:
        sys.stderr.flush()


@app.callback()
def _main() -> None:
    # Logs go to stderr so stdout stays machine-readable (mint prints JSON).
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=_Stderr()))


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code)


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(argus_mint.__version__)


@app.command()
def templates(directory: Annotated[Path, typer.Argument(help="Directory of template YAML files")]) -> None:
    """Validate every template under DIRECTORY and list id, version, status and hash."""
    try:
        reg = load_registry(directory)
    except TemplateError as e:
        raise _fail(str(e)) from e
    for (tid, ver), t in sorted(reg.items()):
        typer.echo(f"{tid}\tv{ver}\t{t.status}\t{hex0x(t.hash())}")


@app.command()
def preflight(
    template: Annotated[Path, typer.Argument(help="Template YAML file (any status)")],
    darkroom: DarkroomOpt = None,
    policy: PolicyOpt = "default",
    limit: Annotated[int, typer.Option(help="Refuse templates with more combinations than this")] = 256,
) -> None:
    """Run darkroom's text pre-flight on every variable combination of TEMPLATE.

    For reviewers: do this before approving a template. Exits 1 if any combination is refused.
    """
    if not darkroom:
        raise _fail("--darkroom (or $ARGUS_MINT_DARKROOM_URL) is required")
    try:
        t = load_template(template)
    except TemplateError as e:
        raise _fail(str(e)) from e
    combos = combinations(t)
    if len(combos) > limit:
        raise _fail(f"{len(combos)} combinations exceed --limit {limit}")
    client = DarkroomClient(darkroom, policy=policy)
    refused = 0
    for values in combos:
        try:
            verdict = client.preflight(fill_for_review(t, values))
        except DarkroomError as e:
            raise _fail(str(e)) from e
        ok = verdict.get("outcome") == "ok"
        refused += not ok
        detail = "" if ok else f"\t{verdict.get('refusal') or verdict.get('outcome')}"
        typer.echo(f"{'ok' if ok else 'REFUSED'}\t{json.dumps(values, sort_keys=True)}{detail}")
    typer.echo(f"{len(combos) - refused}/{len(combos)} combinations pass pre-flight under {policy!r}", err=True)
    if refused:
        raise typer.Exit(1)


class _PlaceholderRenderer:
    """Dry-run stand-in for darklight: the 'image' is the canonical render request."""

    def generate(self, req: RenderRequest) -> GenResult:
        return GenResult.ok(b"argus-mint dry-run placeholder\n" + canonical(req.model_dump(mode="json")))


def _parse_vars(pairs: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for p in pairs:
        key, sep, value = p.partition("=")
        if not sep or not key:
            raise typer.BadParameter(f"expected key=value, got {p!r}", param_hint="--var")
        if key in values:
            raise typer.BadParameter(f"{key} given twice", param_hint="--var")
        values[key] = value
    return values


IpfsOpt = Annotated[
    str | None,
    typer.Option(envvar="ARGUS_MINT_IPFS_API", help="Kubo RPC URL for pinning, e.g. http://127.0.0.1:5001"),
]
BioOpt = Annotated[Path | None, typer.Option(help="Bio template YAML: a language model writes the card's bio")]
WriterOpt = Annotated[
    str | None,
    typer.Option(envvar="ARGUS_MINT_WRITER_URL", help="Bio writer base URL (OpenAI-style POST /chat)"),
]
TextOpt = Annotated[
    list[str],
    typer.Option("--text", help="Display text for a frame slot, slot=text (repeat), e.g. class=Ranger"),
]


def _attempts_json(attempts: Any) -> list[dict[str, Any]]:
    return [{"seed": a.seed, "outcome": str(a.outcome), "ref": a.ref, "detail": a.detail} for a in attempts]


def _render_failed(e: RenderError) -> typer.Exit:
    for a in e.attempts:
        typer.echo(f"  attempt seed={a.seed} {a.outcome} {a.ref} {a.detail}", err=True)
    return _fail(str(e))


def _parse_texts(pairs: list[str]) -> dict[str, str]:
    texts: dict[str, str] = {}
    for p in pairs:
        key, sep, value = p.partition("=")
        if not sep or not key:
            raise typer.BadParameter(f"expected slot=text, got {p!r}", param_hint="--text")
        if key in ("name", "bio"):
            raise typer.BadParameter(f"the {key} slot is filled by argus-mint", param_hint="--text")
        if key in texts:
            raise typer.BadParameter(f"{key} given twice", param_hint="--text")
        texts[key] = value
    return texts


def _writer(writer_url: str | None) -> ChatWriter:
    if not writer_url:
        raise _fail("--bio needs --writer (or $ARGUS_MINT_WRITER_URL)", 2)
    return ChatWriter(writer_url, api_key=os.environ.get(WRITER_KEY_ENV) or None)


@app.command()
def frames(directory: Annotated[Path, typer.Argument(help="Directory of frame YAML files")]) -> None:
    """Validate every frame under DIRECTORY and list id, version, status, size, text slots and hash."""
    files = sorted([*directory.rglob("*.yaml"), *directory.rglob("*.yml")])
    for f in files:
        try:
            lf = load_frame(f)
        except FrameError as e:
            raise _fail(str(e)) from e
        w, h = lf.size
        slots = ",".join(lf.frame.texts) or "-"
        typer.echo(f"{lf.frame.id}\tv{lf.frame.version}\t{lf.frame.status}\t{w}x{h}\t{slots}\t{hex0x(lf.hash())}")


@app.command()
def sample(
    template: Annotated[Path, typer.Argument(help="Template YAML file (any status: drafts are what this is for)")],
    out: Annotated[Path, typer.Option(help="Directory for the sample images")],
    count: Annotated[int, typer.Option(min=1, max=64, help="How many combinations to render")] = 6,
    var: Annotated[list[str], typer.Option(help="Fix a variable to key=value (repeat); the rest vary")] = [],  # noqa: B006
    seed: Annotated[int, typer.Option(help="Picks the combinations, and the first render seed")] = 0,
    frame: Annotated[Path | None, typer.Option(help="Frame YAML to preview (any status)")] = None,
    name: Annotated[str, typer.Option(help="Name drawn in the frame's name slot")] = "Sample",
    bio: BioOpt = None,
    writer: WriterOpt = None,
    text: TextOpt = [],  # noqa: B006
    darkroom: DarkroomOpt = None,
    policy: PolicyOpt = "default",
) -> None:
    """Render sample cards of TEMPLATE for its reviewer. Local files only: never pins, signs or mints.

    Picks COUNT combinations (deterministic for --seed) among those matching --var, renders
    each through darkroom with the usual safety check and retries, and writes the images to
    --out. With --frame it also writes framed previews; --bio adds a written bio (any status
    of template, frame or bio template may be previewed). Prints a JSON summary.
    """
    if not darkroom:
        raise _fail("--darkroom (or $ARGUS_MINT_DARKROOM_URL) is required")
    try:
        t = load_template(template)
        fixed = _parse_vars(var)
        extra = _parse_texts(text)
        lf = load_frame(frame) if frame else None
        bt = load_bio(bio) if bio else None
    except (TemplateError, FrameError, BioError) as e:
        raise _fail(str(e)) from e
    if extra and lf is None:
        raise typer.BadParameter("display text needs a --frame to draw it on", param_hint="--text")
    bio_writer = _writer(writer) if bt else None
    unknown = fixed.keys() - t.variables.keys()
    if unknown:
        raise typer.BadParameter(f"unknown variables: {sorted(unknown)}", param_hint="--var")
    pool = [c for c in combinations(t) if all(c[k] == v for k, v in fixed.items())]
    if not pool:
        raise _fail("no combination matches the fixed --var values")
    picks = random.Random(seed).sample(pool, min(count, len(pool)))
    client = DarkroomClient(darkroom, policy=policy)
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for i, values in enumerate(picks, 1):
        entry: dict[str, Any] = {"n": i, "variables": values}
        try:
            r = render_for_review(client, t, values, seed + i)
            b = write_bio(bio_writer, client, bt, values, t, name, review=True) if bt and bio_writer else None
        except (DarkroomError, BioFailed) as e:
            if isinstance(e, BioFailed):
                entry |= {"error": str(e), "bio_attempts": bio_attempts(e.attempts)}
                results.append(entry)
                typer.echo(f"{i}: {e}", err=True)
                continue
            raise _fail(str(e)) from e
        except BioError as e:
            raise _fail(str(e)) from e
        except RenderError as e:
            entry |= {"error": str(e), "attempts": _attempts_json(e.attempts)}
            results.append(entry)
            typer.echo(f"{i}: {e}", err=True)
            continue
        art = out / f"{i:02d}-{r.seed}.png"
        art.write_bytes(r.image)
        entry |= {"prompt": r.request.prompt, "seed": r.seed, "attempts": _attempts_json(r.attempts), "art": str(art)}
        if b is not None:
            entry |= {"bio": b.text, "bio_attempts": bio_attempts(b.attempts)}
        if lf is not None:
            framed = out / f"{i:02d}-{r.seed}-framed.png"
            try:
                framed.write_bytes(compose(lf, r.image, card_texts(lf, name, b.text if b else None, extra)))
            except FrameError as e:
                raise _fail(str(e)) from e
            entry["framed"] = str(framed)
        results.append(entry)
        typer.echo(f"{i}: {entry.get('framed', art)}", err=True)
    typer.echo(json.dumps({"template": f"{t.id} v{t.version}", "status": t.status, "samples": results}, indent=2))


@app.command()
def draft(
    template: Annotated[Path, typer.Option(help="Template YAML file (must be approved)")],
    out: Annotated[Path, typer.Option(help="Draft directory to create (art.png, card.png, draft.json)")],
    to: Annotated[str, typer.Option(help="Address the card will be minted to (the owner who reviews it)")],
    name: Annotated[str, typer.Option(help="Card name (free text: metadata and image only, never sent to a model)")],
    var: Annotated[list[str], typer.Option(help="Template variable key=value (repeat)")] = [],  # noqa: B006
    seed: Annotated[int, typer.Option(help="First render seed")] = 0,
    asset_id: Annotated[str | None, typer.Option(help="0x 32-byte asset id")] = None,
    asset: Annotated[str | None, typer.Option(help="Asset key; asset id = keccak256(utf8 key)")] = None,
    description: Annotated[str, typer.Option(help="Metadata description (default: the bio, if one is written)")] = "",
    consumer: Annotated[str, typer.Option(help="Consumer name recorded in provenance")] = "argus-mint",
    external_url: Annotated[str | None, typer.Option()] = None,
    frame: Annotated[Path | None, typer.Option(help="Frame YAML to composite on top (must be approved)")] = None,
    bio: BioOpt = None,
    writer: WriterOpt = None,
    text: TextOpt = [],  # noqa: B006
    image: Annotated[Path | None, typer.Option(help="Use this image instead of rendering")] = None,
    darkroom: DarkroomOpt = None,
    policy: PolicyOpt = "default",
    accept_review: Annotated[bool, typer.Option(help="Accept darkroom 'review' images (default: retry)")] = False,
    placeholder: Annotated[
        bool, typer.Option("--placeholder", help="Placeholder art (for dry runs; publish needs --dry-run)")
    ] = False,
) -> None:
    """Step 1 of 3: render a card, write its bio and frame it into a local draft for review.

    The art comes from darkroom (--darkroom), a file (--image) or a placeholder. With --bio, a
    language model at --writer writes the bio, screened by darkroom's text check (so --bio
    needs --darkroom too). --text fills the frame's other slots (class, stats...) with the
    consumer's display text. Nothing is pinned or signed: next the moderator and the owner
    each run `argus-mint review`, then `argus-mint publish`.
    """
    if (asset_id is None) == (asset is None):
        raise typer.BadParameter("give exactly one of --asset-id or --asset")
    try:
        aid = to_bytes32(asset_id) if asset_id is not None else keccak256(asset.encode())  # type: ignore[union-attr]
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--asset-id") from e
    if image is not None and placeholder:
        raise _fail("give one of --image or --placeholder, not both", 2)
    if image is None and not placeholder and not darkroom:
        raise _fail("give --darkroom (render), --image or --placeholder", 2)
    if bio and not darkroom:
        raise _fail("--bio needs --darkroom: darkroom's text check screens every bio", 2)
    extra = _parse_texts(text)
    if extra and frame is None:
        raise typer.BadParameter("display text needs a --frame to draw it on", param_hint="--text")
    if (out / DRAFT_FILE).exists():
        raise _fail(f"{out} already holds a draft; use a new directory")
    bio_writer = _writer(writer) if bio else None
    client = DarkroomClient(darkroom, policy=policy, accept_review=accept_review) if darkroom else None
    try:
        made = create_draft(
            template=load_template(template),
            values=_parse_vars(var),
            seed=seed,
            asset_id=aid,
            to=to,
            name=name,
            renderer=None if image is not None else (_PlaceholderRenderer() if placeholder else client),
            renderer_name="placeholder" if placeholder else "darkroom",
            image=image.read_bytes() if image is not None else None,
            image_ref=str(image) if image is not None else "",
            description=description,
            external_url=external_url,
            consumer=consumer,
            frame=load_frame(frame) if frame else None,
            bio=load_bio(bio) if bio else None,
            writer=bio_writer,
            guard=client,
            texts=extra,
        )
    except (TemplateError, FrameError) as e:
        raise _fail(str(e)) from e
    except BioFailed as e:
        for a in e.attempts:
            typer.echo(f"  bio attempt {a.outcome} {a.detail}: {a.text}", err=True)
        raise _fail(str(e)) from e
    except (DarkroomError, BioError) as e:
        raise _fail(str(e)) from e
    except RenderError as e:
        raise _render_failed(e) from e
    d = made.draft
    save_draft(out, d, made.art, made.image)
    typer.echo(
        json.dumps(
            {
                "draft": str(out),
                "image": str(out / (CARD_FILE if d.frame else ART_FILE)),
                "image_sha256": d.image_sha256,
                "prompt": d.request.prompt,
                "seed": d.request.seed,
                "attempts": list(d.attempts),
                "bio": d.bio.text if d.bio else None,
                "status": d.status,
                "awaiting": d.awaiting(),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


@app.command("review")
def review_cmd(
    draft_dir: Annotated[Path, typer.Argument(help="Draft directory (from `argus-mint draft`)")],
    role: Annotated[str, typer.Option("--as", help="Who is reviewing: moderator | owner")],
    by: Annotated[str, typer.Option(help="Reviewer's name or id, recorded in the draft")],
    approve: Annotated[bool, typer.Option("--approve/--reject", help="The decision")] = ...,  # type: ignore[assignment]
    note: Annotated[str, typer.Option(help="Reason, especially for a rejection")] = "",
) -> None:
    """Step 2 of 3: approve or reject a draft's card image (look at it first).

    The moderator and the owner each review once. Both must approve before `publish`; a
    rejection closes the draft. The decision is bound to the image's sha256.
    """
    if role not in REVIEWERS:
        raise typer.BadParameter(f"must be one of {', '.join(REVIEWERS)}", param_hint="--as")
    try:
        d, _art, image = load_draft(draft_dir)
        if sha256_hex(image) != d.image_sha256:
            raise DraftError("the card image on disk differs from the one rendered for this draft")
        d = review(d, role=role, approve=approve, by=by, note=note)  # type: ignore[arg-type]
    except DraftError as e:
        raise _fail(str(e)) from e
    save_draft(draft_dir, d)
    typer.echo(json.dumps({"draft": str(draft_dir), "status": d.status, "awaiting": d.awaiting()}, indent=2))


@app.command()
def publish(
    draft_dir: Annotated[Path, typer.Argument(help="Approved draft directory")],
    card: Annotated[str, typer.Option(help="Card contract address (EIP-712 verifyingContract)")],
    network: Annotated[str, typer.Option(help="base | base-sepolia | anvil (sets the chain id)")] = "base-sepolia",
    chain_id: Annotated[int | None, typer.Option(help="Chain id override (default: from --network)")] = None,
    price: Annotated[int, typer.Option(help="Mint price in wei")] = 0,
    ttl: Annotated[int, typer.Option(help="Voucher lifetime in seconds")] = 3600,
    deadline: Annotated[int | None, typer.Option(help="Voucher deadline (unix seconds); overrides --ttl")] = None,
    ipfs_api: IpfsOpt = None,
    account: AccountOpt = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Pin locally (no IPFS); allow a throwaway signer")] = False,
) -> None:
    """Step 3 of 3: pin an approved draft, build its metadata and sign the mint voucher; print JSON.

    Refuses a draft the moderator and the owner haven't both approved, or whose files changed
    since. A live run pins via --ipfs-api and signs with --account (a Foundry keystore) or
    $ARGUS_MINT_SIGNER_KEY. Send the result on-chain with `argus-mint submit`.
    """
    try:
        resolved_chain_id = chain_id if chain_id is not None else networks.get(network).chain_id
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--network") from e
    try:
        d, art, image = load_draft(draft_dir)
        check_publishable(d, art, image)
    except DraftError as e:
        raise _fail(str(e)) from e
    try:
        key = load_signer(account)
    except KeyLoadError as e:
        raise _fail(str(e)) from e
    if not dry_run:
        if d.renderer == "placeholder":
            raise _fail("this draft has placeholder art; only a dry run can publish it", 2)
        missing = [f for f, have in (("--ipfs-api", ipfs_api), (f"--account or ${KEY_ENV}", key)) if not have]
        if missing:
            raise _fail(f"a live publish needs {', '.join(missing)} (or use --dry-run)", 2)
    signer = key or Account.create()
    pinner: Pinner = KuboPinner(ipfs_api) if (ipfs_api and not dry_run) else LocalPinner()
    try:
        result = publish_draft(
            d, art, image, pinner=pinner, signer=signer, chain_id=resolved_chain_id, card=card,
            price=price, deadline=deadline, ttl=ttl,
        )  # fmt: skip
    except PinError as e:
        raise _fail(str(e)) from e
    out = {"dry_run": dry_run, "draft": str(draft_dir), "signer_is_ephemeral": key is None, **result}
    typer.echo(json.dumps(out, indent=2, ensure_ascii=False))


# --- on-chain: submit a voucher, freeze metadata, anchor a Merkle root ---------------------
# Each needs the `chain` extra and a sending account (--account or $ARGUS_MINT_SIGNER_KEY).
# Output is JSON on stdout: the tx hash, block, explorer link and what changed.


def _chain(network: str, rpc_url: str | None) -> tuple[Any, networks.Network, Any]:
    try:
        from argus_mint import chain
    except ImportError as e:
        raise _fail(str(e)) from e
    try:
        net = networks.get(network)
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--network") from e
    try:
        w3 = chain.connect(net, [rpc_url] if rpc_url else None)
    except chain.NoRpcAvailable as e:
        raise _fail(str(e)) from e
    return chain, net, w3


def _sender(account: str | None) -> Any:
    try:
        sender = load_signer(account)
    except KeyLoadError as e:
        raise _fail(str(e)) from e
    if sender is None:
        raise _fail(f"needs a sending account: --account (Foundry keystore) or ${KEY_ENV}", 2)
    return sender


def _transact(chain: Any, w3: Any, c: Any, fn: Any, sender: Any, value: int = 0) -> Any:
    from web3.exceptions import ContractLogicError, Web3Exception

    try:
        return chain.send(w3, fn, sender, value=value)
    except ContractLogicError as e:
        raise _fail(f"the contract refused: {chain.explain_revert(c, e)}") from e
    except (chain.TxFailed, Web3Exception, ValueError) as e:  # e.g. insufficient funds for gas
        raise _fail(f"transaction failed: {e}") from e


def _tx(net: networks.Network, receipt: Any, sender: Any) -> dict[str, Any]:
    tx_hash = hex0x(bytes(receipt["transactionHash"]))
    return {
        "network": net.name,
        "from": sender.address,
        "tx": tx_hash,
        "block": receipt["blockNumber"],
        "gas_used": receipt["gasUsed"],
        "explorer": net.tx_url(tx_hash),
    }


def _read_input(path: Path) -> str:
    return sys.stdin.read() if str(path) == "-" else path.read_text()


@app.command()
def submit(
    mint_json: Annotated[Path, typer.Argument(help="JSON printed by `argus-mint publish` ('-' for stdin)")],
    network: NetworkOpt = "base-sepolia",
    rpc_url: RpcOpt = None,
    account: AccountOpt = None,
) -> None:
    """Send a signed mint voucher to its Card contract; print the new token id.

    Any account may submit a voucher (normally the player's wallet); it pays the gas and the
    voucher's price. The card goes to the voucher's `to` address, not the sender.
    """
    try:
        data = json.loads(_read_input(mint_json))
        voucher = MintVoucher.model_validate(data["voucher"])
        sig = bytes.fromhex(data["signature"].removeprefix("0x"))
        dom = data["typed_data"]["domain"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise _fail(f"not a `publish` output: {e}") from e
    if data.get("dry_run"):
        raise _fail("this voucher is from a dry run (local pins, maybe placeholder art); publish it live first")
    chain, net, w3 = _chain(network, rpc_url)
    if dom["chainId"] != net.chain_id:
        raise _fail(f"the voucher is for chain {dom['chainId']}, but {net.name} is chain {net.chain_id}")
    card = chain.contract(w3, "Card", dom["verifyingContract"])
    domain = Domain(chain_id=dom["chainId"], verifying_contract=dom["verifyingContract"])

    # Check what the contract would refuse first, so the error says why (and costs no gas).
    signer = recover(voucher, domain, sig)
    if not card.functions.hasRole(card.functions.SIGNER_ROLE().call(), signer).call():
        raise _fail(f"the voucher's signer {signer} does not hold SIGNER_ROLE on {card.address}")
    if existing := card.functions.tokenOfAsset(voucher.asset_id).call():
        raise _fail(f"asset {hex0x(voucher.asset_id)} is already minted as token {existing}")
    if voucher.deadline < w3.eth.get_block("latest")["timestamp"]:
        raise _fail(f"the voucher expired at {voucher.deadline}; sign a new one")

    sender = _sender(account)
    v = voucher
    fn = card.functions.mint((v.to, v.asset_id, v.content_hash, v.price, v.deadline), sig)
    receipt = _transact(chain, w3, card, fn, sender, value=v.price)

    from web3.logs import DISCARD

    minted = card.events.Minted().process_receipt(receipt, errors=DISCARD)
    token_id = minted[0]["args"]["tokenId"] if minted else None
    out = {
        **_tx(net, receipt, sender),
        "card": card.address,
        "token_id": token_id,
        "to": v.to,
        "asset_id": hex0x(v.asset_id),
        "metadata_uri": f"ipfs://{data['metadata_cid']}" if data.get("metadata_cid") else None,
        "token_url": f"{net.explorer}/nft/{card.address}/{token_id}" if net.explorer and token_id else None,
    }
    typer.echo(json.dumps(out, indent=2))


@app.command()
def freeze(
    card: Annotated[str, typer.Option(help="Card contract address")],
    token_id: Annotated[int, typer.Option(help="Token to freeze")],
    uri: Annotated[str, typer.Option(help="Final metadata: ipfs://<cid> or a bare CID")],
    network: NetworkOpt = "base-sepolia",
    rpc_url: RpcOpt = None,
    account: AccountOpt = None,
) -> None:
    """Fix a card's metadata at an IPFS URI for good (METADATA_ROLE; once per token).

    After this, tokenURI returns URI, wallets and explorers can show the card, and the
    metadata can no longer be refreshed.
    """
    uri = uri if uri.startswith("ipfs://") else f"ipfs://{uri}"
    chain, net, w3 = _chain(network, rpc_url)
    c = chain.contract(w3, "Card", card)
    if c.functions.isFrozen(token_id).call():
        raise _fail(f"token {token_id} is already frozen at {c.functions.tokenURI(token_id).call()}")
    sender = _sender(account)
    receipt = _transact(chain, w3, c, c.functions.freeze(token_id, uri), sender)
    out = {**_tx(net, receipt, sender), "card": c.address, "token_id": token_id, "token_uri": uri}
    typer.echo(json.dumps(out, indent=2))


@app.command()
def anchor(
    leaves: Annotated[Path, typer.Argument(help="File of 32-byte 0x leaf values, one per line ('-' for stdin)")],
    ledger: Annotated[str, typer.Option(help="Ledger contract address")],
    epoch: Annotated[int, typer.Option(help="Epoch to anchor (once per epoch)", min=0, max=2**64 - 1)],
    tree_out: Annotated[
        Path | None, typer.Option("--tree", help="Write the tree (standard-v1 dump, for proofs) here")
    ] = None,
    network: NetworkOpt = "base-sepolia",
    rpc_url: RpcOpt = None,
    account: AccountOpt = None,
) -> None:
    """Build a Merkle tree over LEAVES and anchor its root in the Ledger (COMMITTER_ROLE).

    Leaves are the consumer's 32-byte record hashes; blank lines and # comments are skipped.
    Keep the --tree dump: proofs for individual records come from it.
    """
    try:
        values = [
            ln.strip() for ln in _read_input(leaves).splitlines() if ln.strip() and not ln.lstrip().startswith("#")
        ]
        tree = merkle.build(values)
    except (OSError, ValueError) as e:
        raise _fail(f"bad leaves: {e}") from e
    if len(tree) > 2**32 - 1:
        raise _fail("too many leaves for one anchor (uint32 count)")
    chain, net, w3 = _chain(network, rpc_url)
    c = chain.contract(w3, "Ledger", ledger)
    root, count, at = c.functions.root(epoch).call()
    if at:
        raise _fail(f"epoch {epoch} is already anchored (root {hex0x(bytes(root))}, {count} leaves)")
    if tree_out is not None:  # written before sending, so a landed anchor always has its tree
        tree_out.write_text(json.dumps(tree.dump(), indent=2) + "\n")
    sender = _sender(account)
    receipt = _transact(chain, w3, c, c.functions.anchor(epoch, tree.root, len(tree)), sender)
    out = {
        **_tx(net, receipt, sender),
        "ledger": c.address,
        "epoch": epoch,
        "root": hex0x(tree.root),
        "count": len(tree),
        "tree": str(tree_out) if tree_out else None,
    }
    typer.echo(json.dumps(out, indent=2))


# --- service: the card workflow over HTTP, and its wire schema ------------------------------
# Needs the `server` extra. Configuration is ARGUS_MINT_* (see argus_mint.server.Settings).

WIRE_SCHEMA_PATH = Path("schema") / "mint-wire.schema.json"


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address (default: localhost only)")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port")] = 8108,
    cors: Annotated[
        str | None, typer.Option(help="Comma-separated browser origins allowed to call it (overrides the env)")
    ] = None,
) -> None:
    """Serve drafts, reviews and publishing over HTTP for argus-studio and argus-assay.

    Configured by ARGUS_MINT_* variables: DRAFTS_DIR, TEMPLATES_DIR, FRAMES_DIR, BIOS_DIR,
    DARKROOM_URL, WRITER_URL, IPFS_API, ACCOUNT (+ KEYSTORE_PASSWORD_FILE) or SIGNER_KEY,
    NETWORK, CARD, MODERATOR_TOKEN, SIWE_DOMAIN, CORS_ORIGINS, READ_ONLY.
    """
    try:
        import uvicorn

        from argus_mint.server import Settings, create_app
    except ImportError as e:
        raise _fail(f'the server needs the server extra: pip install "argus-mint[server]" ({e})') from e
    settings = Settings.from_env()
    if cors is not None:
        from dataclasses import replace

        settings = replace(settings, cors_origins=tuple(o.strip() for o in cors.split(",") if o.strip()))
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


@app.command()
def schema(
    check: Annotated[bool, typer.Option("--check", help="Fail if the committed schema is out of date")] = False,
    path: Annotated[Path, typer.Option(help="Schema file")] = WIRE_SCHEMA_PATH,
) -> None:
    """Write (or --check) the HTTP wire schema that argus-studio and argus-assay pin."""
    try:
        from argus_cortex.wire import render_schema

        from argus_mint.wire import WIRE_MODELS, WIRE_VERSION
    except ImportError as e:
        raise _fail(f'the schema needs the server extra: pip install "argus-mint[server]" ({e})') from e
    text = render_schema(WIRE_MODELS, title=f"argus-mint wire {WIRE_VERSION}")
    if check:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != text:
            raise _fail(f"{path} is out of date: run `argus-mint schema`")
        typer.echo(f"{path} is up to date", err=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    typer.echo(str(path), err=True)
