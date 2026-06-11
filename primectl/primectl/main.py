"""Typer root application for primectl."""

from __future__ import annotations

import typer

app = typer.Typer(
    name="primectl",
    help="kubectl-style CLI for the Primer API.",
    no_args_is_help=True,
    add_completion=True,
)


@app.callback()
def _main() -> None:
    """primectl -- kubectl-style CLI for the Primer API."""


@app.command()
def version() -> None:
    """Print the primectl version."""
    from primectl import __version__

    typer.echo(__version__)


if __name__ == "__main__":
    app()
