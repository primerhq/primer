"""Python-toolset commands, keeping primectl in parity with the REST surface.

Source is read from a FILE rather than an argument. A python module is
multi-line and quoting one through a shell is how you end up registering
something subtly different from what you wrote.
"""

from __future__ import annotations

import pathlib

import typer

from primectl.client import ApiError, ConnectionFailed
from primectl.commands.crud import _fail, _session
from primectl.output import render

toolset_app = typer.Typer(
    name="toolset",
    help="Toolset convenience commands (python toolset authoring).",
    no_args_is_help=True,
)


def _emit(sess, data) -> None:
    fmt = sess.output if sess.output in ("json", "yaml") else "yaml"
    typer.echo(render(data, fmt=fmt))


def _read_source(source_file: str) -> str:
    path = pathlib.Path(source_file)
    if not path.is_file():
        typer.echo(f"no such file: {source_file}", err=True)
        raise typer.Exit(2)
    return path.read_text(encoding="utf-8")


@toolset_app.command("create-python")
def create_python(
    ctx: typer.Context,
    toolset_id: str = typer.Option(..., "--id", help="Id for the new toolset."),
    source_file: str = typer.Option(
        ..., "--source-file", help="Path to the python module to register."
    ),
    timeout_seconds: float = typer.Option(
        30.0,
        "--timeout-seconds",
        help="Default wall-clock ceiling for tools that declare none.",
    ),
    allow_network: bool = typer.Option(
        False, "--allow-network", help="Permit outbound network from the tools."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Read and validate the file locally without calling the server.",
    ),
) -> None:
    """Register a python module as a toolset."""
    source = _read_source(source_file)
    body = {
        "id": toolset_id,
        "provider": "python",
        "config": {
            "source": source,
            "source_version": 1,
            "default_timeout_seconds": timeout_seconds,
            "allow_network": allow_network,
        },
    }
    if dry_run:
        _emit(
            _session(ctx),
            {"ok": True, "dry_run": True, "id": toolset_id,
             "source_bytes": len(source)},
        )
        return
    sess = _session(ctx)
    try:
        resp = sess.client.request("post", "/v1/toolsets", json=body)
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    _emit(sess, resp.json())


@toolset_app.command("update-python-source")
def update_python_source(
    ctx: typer.Context,
    toolset_id: str = typer.Option(..., "--id", help="Id of the toolset to edit."),
    source_file: str = typer.Option(
        ..., "--source-file", help="Path to the replacement python module."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Read the file without calling the server."
    ),
) -> None:
    """Replace a python toolset's source.

    The server owns ``source_version`` and bumps it when the source actually
    changes, so a session parked in one of these tools resumes against the code
    that parked.
    """
    source = _read_source(source_file)
    if dry_run:
        _emit(
            _session(ctx),
            {"ok": True, "dry_run": True, "id": toolset_id,
             "source_bytes": len(source)},
        )
        return
    sess = _session(ctx)
    try:
        existing = sess.client.request("get", f"/v1/toolsets/{toolset_id}").json()
        config = dict(existing.get("config") or {})
        config["source"] = source
        resp = sess.client.request(
            "put",
            f"/v1/toolsets/{toolset_id}",
            json={"id": toolset_id, "provider": "python", "config": config},
        )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    _emit(sess, resp.json())


@toolset_app.command("list-python-tools")
def list_python_tools(
    ctx: typer.Context,
    toolset_id: str = typer.Option(..., "--id", help="Id of the toolset."),
) -> None:
    """Show the tools a python toolset derives, plus its isolation level."""
    sess = _session(ctx)
    try:
        resp = sess.client.request("get", f"/v1/toolsets/{toolset_id}/runtime")
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    _emit(sess, resp.json())


def register(app: typer.Typer) -> None:
    app.add_typer(toolset_app, name="toolset")
