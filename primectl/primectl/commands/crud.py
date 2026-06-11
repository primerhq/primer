"""Generic CRUD verb commands: get, describe, delete (create/apply/edit added later)."""

from __future__ import annotations

import typer

from primectl.client import ApiError, ConnectionFailed
from primectl.errors import exit_code_for, format_error
from primectl.filters import build_predicate
from primectl.output import derive_columns, render
from primectl.registry import UnknownResource
from primectl.session import Session


def _session(ctx: typer.Context) -> Session:
    obj = ctx.obj
    if not isinstance(obj, Session):
        typer.echo("internal error: no session", err=True)
        raise typer.Exit(1)
    return obj


def _fail(sess: Session, exc: Exception) -> None:
    typer.echo(format_error(exc, server=sess.target.server), err=True)
    raise typer.Exit(exit_code_for(exc))


def _items(payload) -> list:
    if isinstance(payload, dict) and "items" in payload:
        return payload["items"]
    if isinstance(payload, list):
        return payload
    return [payload]


def register(app: typer.Typer) -> None:
    @app.command()
    def get(
        ctx: typer.Context,
        resource: str = typer.Argument(..., help="Resource name or alias."),
        id: str = typer.Argument(None, help="Optional id; omit to list."),
        filter: list[str] = typer.Option(
            None, "--filter", help="field=value or field=op:value (repeatable)."
        ),
        limit: int = typer.Option(None, "--limit", help="Max rows to list."),
        output: str = typer.Option(None, "-o", "--output", help="Output format override."),
    ) -> None:
        """List a resource, or get one by id."""
        sess = _session(ctx)
        if output is not None:
            sess.output = output
        try:
            res = sess.registry.resolve(resource)
        except UnknownResource as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        try:
            if id is not None:
                resp = sess.client.request("get", f"{res.path_prefix}/{id}")
                obj = resp.json()
                _emit(sess, obj, res, single=True)
                return
            if filter:
                pred = build_predicate(list(filter))
                body = {"predicate": pred, "page": None, "order_by": None}
                resp = sess.client.request("post", f"{res.path_prefix}/find", json=body)
            else:
                params = {"limit": limit} if limit is not None else None
                resp = sess.client.request("get", res.path_prefix, params=params)
            _emit(sess, _items(resp.json()), res, single=False)
        except (ApiError, ConnectionFailed) as exc:
            _fail(sess, exc)

    @app.command()
    def describe(
        ctx: typer.Context,
        resource: str = typer.Argument(...),
        id: str = typer.Argument(...),
    ) -> None:
        """Show a single object in full (YAML)."""
        sess = _session(ctx)
        try:
            res = sess.registry.resolve(resource)
            resp = sess.client.request("get", f"{res.path_prefix}/{id}")
        except UnknownResource as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        except (ApiError, ConnectionFailed) as exc:
            _fail(sess, exc)
            return
        typer.echo(render(resp.json(), fmt="yaml"))

    @app.command()
    def delete(
        ctx: typer.Context,
        resource: str = typer.Argument(...),
        id: str = typer.Argument(...),
    ) -> None:
        """Delete a resource by id."""
        sess = _session(ctx)
        try:
            res = sess.registry.resolve(resource)
            sess.client.request("delete", f"{res.path_prefix}/{id}")
        except UnknownResource as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        except (ApiError, ConnectionFailed) as exc:
            _fail(sess, exc)
            return
        typer.echo(f"{res.name}/{id} deleted")


def _emit(sess: Session, data, res, *, single: bool) -> None:
    fmt = sess.output
    if fmt in ("table", "wide"):
        schema = sess.registry.entity_schema(res)
        columns = derive_columns(schema, wide=(fmt == "wide"))
        typer.echo(render(data, fmt=fmt, columns=columns))
    else:
        typer.echo(render(data, fmt=fmt))
