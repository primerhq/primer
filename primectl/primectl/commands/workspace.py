"""Workspace file commands: workspace files get/put/ls/rm.

The workspace file surface is addressed by a workspace-relative ``path`` query
parameter rather than by id, so it does not fit the generic CRUD shape the
registry derives. These thin commands keep primectl in parity with the REST
file routes and replace the ``primectl raw`` fallback with first-class verbs:

* ``GET    /v1/workspaces/{wid}/files?path=<p>``       -> ls (list entries)
* ``GET    /v1/workspaces/{wid}/files/read?path=<p>``  -> get (read one file)
* ``PUT    /v1/workspaces/{wid}/files?path=<p>``       -> put (write/replace)
* ``DELETE /v1/workspaces/{wid}/files?path=<p>``       -> rm (delete)

The sub-app is named ``workspace`` (not ``ws``) to avoid colliding with the
``ws`` generic-resource alias the registry exposes for the workspaces resource.
"""

from __future__ import annotations

import base64
from pathlib import Path

import typer

from primectl.client import ApiError, ConnectionFailed
from primectl.commands.crud import _fail, _session
from primectl.output import render

workspace_app = typer.Typer(
    name="workspace",
    help="Workspace-scoped convenience commands (file get/put/ls/rm).",
    no_args_is_help=True,
)

files_app = typer.Typer(
    name="files",
    help="Read, write, list, and delete files inside a workspace.",
    no_args_is_help=True,
)


def _files_path(workspace_id: str) -> str:
    return f"/v1/workspaces/{workspace_id}/files"


@files_app.command("get")
def get(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(..., help="Workspace id."),
    path: str = typer.Argument(..., help="Workspace-relative file path."),
    encoding: str = typer.Option(
        "text", "--encoding", help="Payload encoding: text|base64."
    ),
    out: str = typer.Option(
        None, "--out", help="Write the file body to this local path instead of stdout."
    ),
    content: bool = typer.Option(
        False, "--content", help="Print only the bare file body."
    ),
    output: str = typer.Option(
        None, "-o", "--output", help="Output: table|json|yaml|name|wide."
    ),
) -> None:
    """Read one workspace file (returns its body + metadata)."""
    sess = _session(ctx)
    if output is not None:
        sess.output = output
    try:
        resp = sess.client.request(
            "get",
            f"{_files_path(workspace_id)}/read",
            params={"path": path, "encoding": encoding},
        )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    data = resp.json()
    if out is not None:
        body = data.get("content", "")
        if data.get("encoding") == "base64":
            Path(out).write_bytes(base64.b64decode(body))
        else:
            Path(out).write_text(body)
        typer.echo(f"wrote {workspace_id}:{path} to {out}")
        return
    if content:
        typer.echo(data.get("content", ""))
        return
    fmt = sess.output if sess.output in ("json", "yaml") else "yaml"
    typer.echo(render(data, fmt=fmt))


@files_app.command("ls")
def ls(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(..., help="Workspace id."),
    path: str = typer.Argument(".", help="Workspace-relative directory path."),
    recursive: bool = typer.Option(
        False, "--recursive", "-R", help="List entries recursively."
    ),
    output: str = typer.Option(
        None, "-o", "--output", help="Output: table|json|yaml|name|wide."
    ),
) -> None:
    """List files at a workspace path (no bodies)."""
    sess = _session(ctx)
    if output is not None:
        sess.output = output
    params: dict = {"path": path}
    if recursive:
        params["recursive"] = recursive
    try:
        resp = sess.client.request("get", _files_path(workspace_id), params=params)
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    rows = resp.json().get("items", [])
    columns = ["path", "kind", "size_bytes", "modified_at"]
    fmt = sess.output
    if fmt in ("table", "wide"):
        typer.echo(render(rows, fmt=fmt, columns=columns))
    else:
        typer.echo(render(rows, fmt=fmt))


@files_app.command("put")
def put(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(..., help="Workspace id."),
    path: str = typer.Argument(..., help="Workspace-relative file path."),
    content: str = typer.Option(
        None, "--content", help="File body (or use --file)."
    ),
    file: str = typer.Option(
        None, "-f", "--file", help="Read the file body from this local path."
    ),
    encoding: str = typer.Option(
        "text", "--encoding", help="Body encoding: text|base64."
    ),
) -> None:
    """Create or replace a workspace file."""
    sess = _session(ctx)
    if content is None and file is None:
        typer.echo("put needs --content or --file", err=True)
        raise typer.Exit(1)
    if content is not None and file is not None:
        typer.echo("put takes --content or --file, not both", err=True)
        raise typer.Exit(1)
    if file is not None:
        if encoding == "base64":
            body_text = base64.b64encode(Path(file).read_bytes()).decode("ascii")
        else:
            body_text = Path(file).read_text()
    else:
        body_text = content
    body: dict = {"content": body_text, "encoding": encoding}
    try:
        sess.client.request(
            "put", _files_path(workspace_id), params={"path": path}, json=body
        )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    typer.echo(f"file {workspace_id}:{path} written")


@files_app.command("rm")
def rm(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(..., help="Workspace id."),
    path: str = typer.Argument(..., help="Workspace-relative file path."),
    recursive: bool = typer.Option(
        False, "--recursive", "-R", help="Delete a non-empty directory recursively."
    ),
) -> None:
    """Delete a workspace file or directory."""
    sess = _session(ctx)
    params: dict = {"path": path}
    if recursive:
        params["recursive"] = recursive
    try:
        sess.client.request("delete", _files_path(workspace_id), params=params)
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    typer.echo(f"file {workspace_id}:{path} deleted")


def register(app: typer.Typer) -> None:
    workspace_app.add_typer(files_app, name="files")
    app.add_typer(workspace_app, name="workspace")
