"""Typer root application for primectl."""

from __future__ import annotations

import typer

from primectl.config import load_config, resolve_target, ConfigError
from primectl.session import Session

app = typer.Typer(
    name="primectl",
    help="kubectl-style CLI for the Primer API.",
    no_args_is_help=True,
    add_completion=True,
)

from primectl.commands import crud as _crud  # noqa: E402
from primectl.commands import call as _call  # noqa: E402
from primectl.commands import raw as _raw  # noqa: E402
from primectl.commands import meta as _meta  # noqa: E402
from primectl.commands import doc as _doc  # noqa: E402
from primectl.commands import channel as _channel  # noqa: E402
from primectl.commands import workspace as _workspace  # noqa: E402
from primectl.commands import chat as _chat  # noqa: E402
from primectl.commands.config_cmd import config_app  # noqa: E402

_crud.register(app)
_call.register(app)
_raw.register(app)
_meta.register(app)
_doc.register(app)
_channel.register(app)
_workspace.register(app)
_chat.register(app)
app.add_typer(config_app, name="config")


@app.callback()
def main(
    ctx: typer.Context,
    context: str = typer.Option(None, "--context", help="Named context to use."),
    server: str = typer.Option(None, "--server", help="Override the server URL."),
    token: str = typer.Option(None, "--token", help="Override the bearer token."),
    output: str = typer.Option(
        "table", "-o", "--output", help="Output: table|json|yaml|name|wide."
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Force a spec refresh."),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Print requests."),
) -> None:
    # If a Session was pre-injected (tests pass obj=Session to CliRunner), keep
    # it and just apply the CLI-flag overrides onto it; do NOT re-resolve a
    # target. This is what lets command tests run without a real config.
    if isinstance(ctx.obj, Session):
        ctx.obj.output = output
        ctx.obj.refresh = refresh
        ctx.obj.verbose = verbose
        return
    # Commands that never need a server (e.g. 'version', 'config') skip resolution.
    if ctx.invoked_subcommand in ("version", "config"):
        return
    try:
        cfg = load_config()
        target = resolve_target(
            cfg, context=context, server=server, token=token,
        )
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    ctx.obj = Session(
        target=target, output=output, refresh=refresh, verbose=verbose,
    )


@app.command()
def version() -> None:
    """Print the primectl version."""
    from primectl import __version__

    typer.echo(__version__)


if __name__ == "__main__":
    app()
