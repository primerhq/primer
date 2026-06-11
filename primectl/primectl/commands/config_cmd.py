"""The 'primectl config' sub-app: manage contexts."""

from __future__ import annotations

import typer

from primectl.config import (
    Context, load_config, save_config, config_path,
)

config_app = typer.Typer(name="config", help="Manage primectl contexts.", no_args_is_help=True)


@config_app.command("set-context")
def set_context(
    name: str = typer.Argument(...),
    server: str = typer.Option(None, "--server"),
    token: str = typer.Option(None, "--token"),
    workspace: str = typer.Option(None, "--workspace"),
) -> None:
    """Create or update a named context."""
    cfg = load_config()
    existing = cfg.contexts.get(name)
    cfg.contexts[name] = Context(
        server=server or (existing.server if existing else ""),
        token=token if token is not None else (existing.token if existing else None),
        workspace=workspace if workspace is not None else (existing.workspace if existing else None),
    )
    if cfg.current_context is None:
        cfg.current_context = name
    save_config(cfg)
    typer.echo(f"context {name!r} set")


@config_app.command("use-context")
def use_context(name: str = typer.Argument(...)) -> None:
    """Switch the current context."""
    cfg = load_config()
    if name not in cfg.contexts:
        typer.echo(f"no such context {name!r}", err=True)
        raise typer.Exit(1)
    cfg.current_context = name
    save_config(cfg)
    typer.echo(f"switched to {name!r}")


@config_app.command("get-contexts")
def get_contexts() -> None:
    """List configured contexts."""
    cfg = load_config()
    for name, ctx in cfg.contexts.items():
        marker = "*" if name == cfg.current_context else " "
        typer.echo(f"{marker} {name}\t{ctx.server}")


@config_app.command("current-context")
def current_context() -> None:
    """Print the current context name."""
    cfg = load_config()
    typer.echo(cfg.current_context or "(none)")


@config_app.command("view")
def view() -> None:
    """Print the config with tokens redacted."""
    cfg = load_config()
    typer.echo(f"current-context: {cfg.current_context}")
    typer.echo(f"path: {config_path()}")
    for name, ctx in cfg.contexts.items():
        tok = "REDACTED" if ctx.token else "(none)"
        typer.echo(f"- {name}: server={ctx.server} token={tok} workspace={ctx.workspace}")
