"""Generic CRUD verb commands: get, describe, delete, create, apply, edit."""

from __future__ import annotations

from pathlib import Path

import typer

from primectl.client import ApiError, ConnectionFailed
from primectl.errors import exit_code_for, format_error
from primectl.filters import build_predicate
from primectl.manifest import parse_manifests, dump_envelope, ManifestError
from primectl.output import derive_columns, render
from primectl.registry import UnknownResource
from primectl.session import Session


def _session(ctx: typer.Context) -> Session:
    obj = ctx.obj
    if not isinstance(obj, Session):
        typer.echo("internal error: no session", err=True)
        raise typer.Exit(1)
    return obj


def _require_op(res, op, verb: str) -> None:
    """Exit with a friendly error if the resource does not support a verb."""
    if op is None:
        typer.echo(f"resource {res.name!r} does not support {verb}", err=True)
        raise typer.Exit(1)


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
        raw_output: bool = typer.Option(
            False, "-r", "--raw-output",
            help="For a single object, print the bare body (no kind/spec envelope).",
        ),
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
                fmt = sess.output
                if fmt in ("yaml", "json") and not raw_output:
                    # Emit the kind/spec envelope so the object round-trips
                    # straight into `apply -f`. Use --raw-output for the bare body.
                    typer.echo(dump_envelope(res.name, obj, fmt=fmt))
                else:
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
        output: str = typer.Option(
            None, "-o", "--output", help="Output: yaml (default), json, or name."
        ),
    ) -> None:
        """Show a single object in full (YAML by default)."""
        sess = _session(ctx)
        if output is not None:
            sess.output = output
        try:
            res = sess.registry.resolve(resource)
            resp = sess.client.request("get", f"{res.path_prefix}/{id}")
        except UnknownResource as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        except (ApiError, ConnectionFailed) as exc:
            _fail(sess, exc)
            return
        # A single object detail view: honor json/yaml/name; table/wide are
        # meaningless for one object, so fall back to yaml.
        fmt = sess.output if sess.output in ("json", "yaml", "name") else "yaml"
        typer.echo(render(resp.json(), fmt=fmt))

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
            _require_op(res, res.delete_op, "delete")
            sess.client.request("delete", f"{res.path_prefix}/{id}")
        except UnknownResource as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        except (ApiError, ConnectionFailed) as exc:
            _fail(sess, exc)
            return
        typer.echo(f"{res.name}/{id} deleted")

    @app.command()
    def create(
        ctx: typer.Context,
        resource: str = typer.Argument(None, help="Resource (omit when using -f)."),
        file: str = typer.Option(None, "-f", "--file", help="Manifest file (kind/spec)."),
        set_: list[str] = typer.Option(
            None, "--set", help="field=value pairs (used without -f)."
        ),
    ) -> None:
        """Create a resource from a manifest file or --set pairs."""
        sess = _session(ctx)
        try:
            if file:
                docs = parse_manifests(Path(file).read_text())
                for kind, body in docs:
                    res = sess.registry.resolve(kind)
                    _require_op(res, res.create_op, "create")
                    resp = sess.client.request("post", res.path_prefix, json=body)
                    ident = resp.json().get("id", body.get("id", "?"))
                    typer.echo(f"{res.name}/{ident} created")
                return
            if not resource:
                typer.echo("create needs a resource + --set, or -f FILE", err=True)
                raise typer.Exit(1)
            res = sess.registry.resolve(resource)
            _require_op(res, res.create_op, "create")
            body = _assemble_set(set_ or [])
            resp = sess.client.request("post", res.path_prefix, json=body)
            ident = resp.json().get("id", body.get("id", "?"))
            typer.echo(f"{res.name}/{ident} created")
        except (ManifestError, UnknownResource) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        except (ApiError, ConnectionFailed) as exc:
            _fail(sess, exc)

    @app.command()
    def apply(
        ctx: typer.Context,
        file: str = typer.Option(..., "-f", "--file", help="Manifest file/dir/-."),
    ) -> None:
        """Declaratively upsert objects from a manifest (PUT if present else POST)."""
        sess = _session(ctx)
        try:
            text = _read_manifest_source(file)
            docs = parse_manifests(text)
        except (ManifestError, OSError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        for kind, body in docs:
            try:
                res = sess.registry.resolve(kind)
            except UnknownResource as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1)
            ident = body.get("id")
            if not ident:
                typer.echo(
                    f"apply: {kind} manifest needs spec.id (use 'create' to "
                    "let the server assign one)", err=True,
                )
                raise typer.Exit(1)
            try:
                _apply_one(sess, res, ident, body)
            except (ApiError, ConnectionFailed) as exc:
                _fail(sess, exc)

    @app.command()
    def edit(
        ctx: typer.Context,
        resource: str = typer.Argument(...),
        id: str = typer.Argument(...),
    ) -> None:
        """Fetch an object, open it in $EDITOR, and PUT the result."""
        sess = _session(ctx)
        try:
            res = sess.registry.resolve(resource)
            _require_op(res, res.update_op, "edit")
            current = sess.client.request("get", f"{res.path_prefix}/{id}").json()
        except UnknownResource as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        except (ApiError, ConnectionFailed) as exc:
            _fail(sess, exc)
            return
        edited = typer.edit(dump_envelope(res.name, current, fmt="yaml"))
        if edited is None:
            typer.echo("no changes")
            return
        try:
            docs = parse_manifests(edited)
        except ManifestError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        _, body = docs[0]
        try:
            sess.client.request("put", f"{res.path_prefix}/{id}", json=body)
        except (ApiError, ConnectionFailed) as exc:
            _fail(sess, exc)
            return
        typer.echo(f"{res.name}/{id} configured")


def _emit(sess: Session, data, res, *, single: bool) -> None:
    fmt = sess.output
    if fmt in ("table", "wide"):
        schema = sess.registry.entity_schema(res)
        columns = derive_columns(schema, wide=(fmt == "wide"))
        typer.echo(render(data, fmt=fmt, columns=columns))
    else:
        typer.echo(render(data, fmt=fmt))


def _assemble_set(pairs: list[str]) -> dict:
    from primectl.filters import coerce_value

    body: dict = {}
    for p in pairs:
        if "=" not in p:
            raise typer.BadParameter(f"--set expects field=value, got {p!r}")
        k, _, v = p.partition("=")
        body[k.strip()] = coerce_value(v)
    return body


def _read_manifest_source(source: str) -> str:
    if source == "-":
        import sys

        return sys.stdin.read()
    p = Path(source)
    if p.is_dir():
        parts = []
        for f in sorted(p.glob("*.y*ml")):
            parts.append(f.read_text())
        return "\n---\n".join(parts)
    return p.read_text()


def _apply_one(sess: Session, res, ident: str, body: dict) -> None:
    item_path = f"{res.path_prefix}/{ident}"
    exists = True
    try:
        existing = sess.client.request("get", item_path).json()
    except ApiError as exc:
        if exc.status == 404:
            exists = False
        else:
            raise
    if not exists:
        _require_op(res, res.create_op, "create")
        sess.client.request("post", res.path_prefix, json=body)
        typer.echo(f"{res.name}/{ident} created")
        return
    if all(existing.get(k) == v for k, v in body.items()):
        typer.echo(f"{res.name}/{ident} unchanged")
        return
    _require_op(res, res.update_op, "update")
    sess.client.request("put", item_path, json=body)
    typer.echo(f"{res.name}/{ident} configured")
