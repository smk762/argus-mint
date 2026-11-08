"""Command-line interface (the ``cli`` extra): ``argus-mint`` or ``python -m argus_mint``."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Annotated

import structlog
import typer
from eth_account import Account

import argus_mint
from argus_mint._codec import canonical, hex0x, keccak256, to_bytes32
from argus_mint.metadata import Provenance, card_metadata, content_hash
from argus_mint.pin import LocalPinner
from argus_mint.render import GenResult, render
from argus_mint.templates import RenderRequest, TemplateError, fill, load_registry, load_template
from argus_mint.vouchers import Domain, MintVoucher, sign, typed_data

app = typer.Typer(help="argus-mint: provenance tokens for game assets.", no_args_is_help=True)

SIGNER_ENV = "ARGUS_MINT_SIGNER_KEY"


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
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e
    for (tid, ver), t in sorted(reg.items()):
        typer.echo(f"{tid}\tv{ver}\t{t.status}\t{hex0x(t.hash())}")


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
    chain_id: Annotated[int, typer.Option(help="Chain id of the Card contract")],
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
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Render/pin locally; print voucher and metadata")] = False,
) -> None:
    """Render, pin, build metadata and sign a mint voucher.

    The signer key comes from $ARGUS_MINT_SIGNER_KEY (a throwaway key is used in a dry run
    when it's unset). Live runs need the darklight and pinning clients, which are not wired
    up yet, so only --dry-run is available for now.
    """
    if not dry_run:
        typer.echo("error: live minting needs darklight and pinning configured (not available yet); use --dry-run")
        raise typer.Exit(2)
    if (asset_id is None) == (asset is None):
        raise typer.BadParameter("give exactly one of --asset-id or --asset")
    try:
        aid = to_bytes32(asset_id) if asset_id is not None else keccak256(asset.encode())  # type: ignore[union-attr]
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--asset-id") from e

    key_hex = os.environ.get(SIGNER_ENV)
    signer = Account.from_key(key_hex) if key_hex else Account.create()

    try:
        t = load_template(template)
        values = _parse_vars(var)
        if image is not None:
            req = fill(t, values, seed)
            img, final_seed, attempts = image.read_bytes(), req.seed, 1
            prov_req = req
        else:
            rendered = render(_PlaceholderRenderer(), t, values, seed)
            img, final_seed, attempts, prov_req = (
                rendered.image,
                rendered.seed,
                len(rendered.attempts),
                rendered.request,
            )
    except TemplateError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e

    provenance = Provenance(
        template=prov_req.template_id,
        template_version=prov_req.template_version,
        template_hash=prov_req.template_hash,
        seed=final_seed,
        attempts=attempts,
        asset_id=aid,
        consumer=consumer,
    )
    pinner = LocalPinner()
    image_cid = pinner.pin_bytes(img, f"{t.id}-v{t.version}-{final_seed}.png")
    doc = card_metadata(
        name=name,
        description=description,
        image_cid=image_cid,
        external_url=external_url,
        attributes=[],
        provenance=provenance,
    )
    voucher = MintVoucher(
        to=to,
        asset_id=aid,
        content_hash=content_hash(doc),
        price=price,
        deadline=deadline if deadline is not None else int(time.time()) + ttl,
    )
    domain = Domain(chain_id=chain_id, verifying_contract=card)
    out = {
        "dry_run": True,
        "signer": signer.address,
        "signer_is_ephemeral": key_hex is None,
        "render": {"prompt": prov_req.prompt, "seed": final_seed, "attempts": attempts, "image_cid": image_cid},
        "metadata": doc,
        "metadata_cid": pinner.pin_json(doc, f"{t.id}-metadata.json"),
        "voucher": voucher.model_dump(mode="json"),
        "signature": hex0x(sign(voucher, domain, signer)),
        "typed_data": typed_data(voucher, domain),
    }
    typer.echo(json.dumps(out, indent=2, ensure_ascii=False))
