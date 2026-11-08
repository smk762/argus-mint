"""Command-line interface (the ``cli`` extra): ``argus-mint`` or ``python -m argus_mint``."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import structlog
import typer
from eth_account import Account

import argus_mint
from argus_mint import merkle, networks
from argus_mint._codec import canonical, hex0x, keccak256, to_bytes32
from argus_mint.darkroom import DarkroomClient, DarkroomError
from argus_mint.keys import KEY_ENV, KeyLoadError, load_signer
from argus_mint.metadata import Provenance, card_metadata, content_hash
from argus_mint.pin import KuboPinner, LocalPinner, PinError, Pinner
from argus_mint.render import Darklight, GenResult, RenderError, render
from argus_mint.templates import (
    RenderRequest,
    TemplateError,
    combinations,
    fill,
    fill_for_review,
    load_registry,
    load_template,
)
from argus_mint.vouchers import Domain, MintVoucher, recover, sign, typed_data

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


@app.command()
def mint(
    template: Annotated[Path, typer.Option(help="Template YAML file (must be approved)")],
    to: Annotated[str, typer.Option(help="Address the card is minted to")],
    name: Annotated[str, typer.Option(help="Card name (free text: metadata only, never sent to the model)")],
    card: Annotated[str, typer.Option(help="Card contract address (EIP-712 verifyingContract)")],
    network: Annotated[str, typer.Option(help="base | base-sepolia | anvil (sets the chain id)")] = "base-sepolia",
    chain_id: Annotated[int | None, typer.Option(help="Chain id override (default: from --network)")] = None,
    var: Annotated[list[str], typer.Option(help="Template variable key=value (repeat)")] = [],  # noqa: B006
    seed: Annotated[int, typer.Option(help="First render seed")] = 0,
    asset_id: Annotated[str | None, typer.Option(help="0x 32-byte asset id")] = None,
    asset: Annotated[str | None, typer.Option(help="Asset key; asset id = keccak256(utf8 key)")] = None,
    description: Annotated[str, typer.Option()] = "",
    consumer: Annotated[str, typer.Option(help="Consumer name recorded in provenance")] = "argus-mint",
    external_url: Annotated[str | None, typer.Option()] = None,
    price: Annotated[int, typer.Option(help="Mint price in wei")] = 0,
    ttl: Annotated[int, typer.Option(help="Voucher lifetime in seconds")] = 3600,
    deadline: Annotated[int | None, typer.Option(help="Voucher deadline (unix seconds); overrides --ttl")] = None,
    image: Annotated[Path | None, typer.Option(help="Use this image instead of rendering")] = None,
    darkroom: DarkroomOpt = None,
    policy: PolicyOpt = "default",
    accept_review: Annotated[bool, typer.Option(help="Accept darkroom 'review' images (default: retry)")] = False,
    ipfs_api: Annotated[
        str | None,
        typer.Option(envvar="ARGUS_MINT_IPFS_API", help="Kubo RPC URL for pinning, e.g. http://127.0.0.1:5001"),
    ] = None,
    account: AccountOpt = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Pin locally (no IPFS); allow a throwaway signer")] = False,
) -> None:
    """Render, pin, build metadata and sign a mint voucher; print it all as JSON.

    Renders through darkroom when --darkroom is given (a placeholder image otherwise, dry run
    only). A live run pins to IPFS via --ipfs-api and signs with --account (a Foundry keystore)
    or $ARGUS_MINT_SIGNER_KEY; a dry run pins locally and uses a throwaway signer when neither
    is set. Send the result on-chain with `argus-mint submit`.
    """
    try:
        resolved_chain_id = chain_id if chain_id is not None else networks.get(network).chain_id
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--network") from e
    if (asset_id is None) == (asset is None):
        raise typer.BadParameter("give exactly one of --asset-id or --asset")
    try:
        aid = to_bytes32(asset_id) if asset_id is not None else keccak256(asset.encode())  # type: ignore[union-attr]
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--asset-id") from e

    try:
        key = load_signer(account)
    except KeyLoadError as e:
        raise _fail(str(e)) from e
    if not dry_run:
        missing = [
            flag
            for flag, have in (
                ("--darkroom or --image", darkroom or image),
                ("--ipfs-api", ipfs_api),
                (f"--account or ${KEY_ENV}", key),
            )
            if not have
        ]
        if missing:
            raise _fail(f"a live mint needs {', '.join(missing)} (or use --dry-run)", 2)
    signer = key or Account.create()
    renderer: Darklight = (
        DarkroomClient(darkroom, policy=policy, accept_review=accept_review) if darkroom else _PlaceholderRenderer()
    )
    pinner: Pinner = KuboPinner(ipfs_api) if (ipfs_api and not dry_run) else LocalPinner()

    try:
        t = load_template(template)
        values = _parse_vars(var)
        if image is not None:
            req = fill(t, values, seed)
            img, attempts_out = image.read_bytes(), [{"seed": req.seed, "outcome": "supplied", "ref": str(image)}]
        else:
            rendered = render(renderer, t, values, seed)
            img, req = rendered.image, rendered.request
            attempts_out = [
                {"seed": a.seed, "outcome": str(a.outcome), "ref": a.ref, "detail": a.detail} for a in rendered.attempts
            ]
    except TemplateError as e:
        raise _fail(str(e)) from e
    except DarkroomError as e:
        raise _fail(str(e)) from e
    except RenderError as e:
        for a in e.attempts:
            typer.echo(f"  attempt seed={a.seed} {a.outcome} {a.ref} {a.detail}", err=True)
        raise _fail(str(e)) from e

    provenance = Provenance(
        template=req.template_id,
        template_version=req.template_version,
        template_hash=req.template_hash,
        seed=req.seed,
        attempts=len(attempts_out),
        asset_id=aid,
        consumer=consumer,
    )
    try:
        image_cid = pinner.pin_bytes(img, f"{t.id}-v{t.version}-{req.seed}.png")
        doc = card_metadata(
            name=name,
            description=description,
            image_cid=image_cid,
            external_url=external_url,
            attributes=[],
            provenance=provenance,
        )
        metadata_cid = pinner.pin_json(doc, f"{t.id}-metadata.json")
    except PinError as e:
        raise _fail(str(e)) from e
    voucher = MintVoucher(
        to=to,
        asset_id=aid,
        content_hash=content_hash(doc),
        price=price,
        deadline=deadline if deadline is not None else int(time.time()) + ttl,
    )
    domain = Domain(chain_id=resolved_chain_id, verifying_contract=card)
    out = {
        "dry_run": dry_run,
        "signer": signer.address,
        "signer_is_ephemeral": key is None,
        "render": {
            "renderer": "darkroom" if darkroom and image is None else ("supplied" if image else "placeholder"),
            "prompt": req.prompt,
            "seed": req.seed,
            "attempts": attempts_out,
            "image_cid": image_cid,
        },
        "metadata": doc,
        "metadata_cid": metadata_cid,
        "voucher": voucher.model_dump(mode="json"),
        "signature": hex0x(sign(voucher, domain, signer)),
        "typed_data": typed_data(voucher, domain),
    }
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
    mint_json: Annotated[Path, typer.Argument(help="JSON printed by `argus-mint mint` ('-' for stdin)")],
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
        raise _fail(f"not a `mint` output: {e}") from e
    if data.get("dry_run"):
        raise _fail("this voucher is from a dry run (placeholder art, local pins); mint it live first")
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
