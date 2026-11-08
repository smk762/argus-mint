"""Command-line interface (the `cli` extra)."""

from __future__ import annotations

import typer

import argus_mint

app = typer.Typer(help="argus-mint: provenance tokens for game assets.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(argus_mint.__version__)
