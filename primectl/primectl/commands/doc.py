"""Path-addressed collection-document commands: doc get/put/delete/list/move.

The document surface is nested under a collection and addressed by ``path``
rather than by id, so it does not fit the generic CRUD shape the registry
derives. These thin commands keep primectl in parity with the REST routes:

* ``GET    /v1/collections/{cid}/documents?path=<p>``    -> get one
* ``GET    /v1/collections/{cid}/documents?prefix=<p>``  -> list under a prefix
* ``PUT    /v1/collections/{cid}/documents?path=<p>``    -> create/replace
* ``DELETE /v1/collections/{cid}/documents?path=<p>``    -> delete
* ``POST   /v1/collections/{cid}/documents/move``         -> move from/to
"""

from __future__ import annotations

from pathlib import Path

import typer

from primectl.client import ApiError, ConnectionFailed
from primectl.commands.crud import _fail, _session
from primectl.output import render

doc_app = typer.Typer(
    name="doc",
    help="Address collection documents by path (get/put/list/delete/move).",
    no_args_is_help=True,
)


def _docs_path(collection_id: str) -> str:
    return f"/v1/collections/{collection_id}/documents"


@doc_app.command("get")
def get(
    ctx: typer.Context,
    collection: str = typer.Argument(..., help="Collection id."),
    path: str = typer.Argument(..., help="Document path within the collection."),
    content: bool = typer.Option(
        False, "--content", help="Print only the bare document body."
    ),
    output: str = typer.Option(
        None, "-o", "--output", help="Output: table|json|yaml|name|wide."
    ),
) -> None:
    """Read one document by path (returns its body + metadata)."""
    sess = _session(ctx)
    if output is not None:
        sess.output = output
    try:
        resp = sess.client.request(
            "get", _docs_path(collection), params={"path": path}
        )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    data = resp.json()
    if content:
        typer.echo(data.get("content", ""))
        return
    fmt = sess.output if sess.output in ("json", "yaml") else "yaml"
    typer.echo(render(data, fmt=fmt))


@doc_app.command("list")
def list_(
    ctx: typer.Context,
    collection: str = typer.Argument(..., help="Collection id."),
    prefix: str = typer.Option(
        None, "--prefix", help="Optional path prefix to scope the listing."
    ),
    output: str = typer.Option(
        None, "-o", "--output", help="Output: table|json|yaml|name|wide."
    ),
) -> None:
    """List documents under an optional path prefix (no bodies)."""
    sess = _session(ctx)
    if output is not None:
        sess.output = output
    params = {"prefix": prefix} if prefix is not None else None
    try:
        resp = sess.client.request("get", _docs_path(collection), params=params)
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    rows = resp.json().get("documents", [])
    columns = ["path", "document_id", "size"]
    fmt = sess.output
    if fmt in ("table", "wide"):
        typer.echo(render(rows, fmt=fmt, columns=columns))
    else:
        typer.echo(render(rows, fmt=fmt))


@doc_app.command("put")
def put(
    ctx: typer.Context,
    collection: str = typer.Argument(..., help="Collection id."),
    path: str = typer.Argument(..., help="Document path within the collection."),
    content: str = typer.Option(
        None, "--content", help="Document body (or use --file)."
    ),
    file: str = typer.Option(
        None, "-f", "--file", help="Read the document body from this file."
    ),
    title: str = typer.Option(None, "--title", help="Optional display title."),
) -> None:
    """Create or replace the document at a path."""
    sess = _session(ctx)
    if content is None and file is None:
        typer.echo("put needs --content or --file", err=True)
        raise typer.Exit(1)
    if content is not None and file is not None:
        typer.echo("put takes --content or --file, not both", err=True)
        raise typer.Exit(1)
    body_text = content if content is not None else Path(file).read_text()
    body: dict = {"content": body_text}
    if title is not None:
        body["title"] = title
    try:
        sess.client.request(
            "put", _docs_path(collection), params={"path": path}, json=body
        )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    typer.echo(f"document {collection}/{path} configured")


@doc_app.command("delete")
def delete(
    ctx: typer.Context,
    collection: str = typer.Argument(..., help="Collection id."),
    path: str = typer.Argument(..., help="Document path within the collection."),
) -> None:
    """Delete a document by path."""
    sess = _session(ctx)
    try:
        sess.client.request(
            "delete", _docs_path(collection), params={"path": path}
        )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    typer.echo(f"document {collection}/{path} deleted")


@doc_app.command("move")
def move(
    ctx: typer.Context,
    collection: str = typer.Argument(..., help="Collection id."),
    from_: str = typer.Argument(..., metavar="FROM", help="Source path."),
    to: str = typer.Argument(..., metavar="TO", help="Destination path."),
) -> None:
    """Move a document from one path to another within the collection."""
    sess = _session(ctx)
    try:
        sess.client.request(
            "post",
            f"{_docs_path(collection)}/move",
            json={"from": from_, "to": to},
        )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    typer.echo(f"document {collection}/{from_} moved to {to}")


def register(app: typer.Typer) -> None:
    app.add_typer(doc_app, name="doc")
