"""Spec-driven invoker for non-CRUD custom operations: primectl call <res> <action>."""

from __future__ import annotations

import typer

from primectl.client import ApiError, ConnectionFailed
from primectl.commands.crud import _fail, _session
from primectl.output import render
from primectl.registry import UnknownResource


def register(app: typer.Typer) -> None:
    @app.command()
    def call(
        ctx: typer.Context,
        resource: str = typer.Argument(...),
        action: str = typer.Argument(None, help="Custom action (omit to list)."),
        id: str = typer.Argument(None, help="Resource id, if the op needs one."),
        param: list[str] = typer.Option(
            None, "--param", help="path/query param key=value (repeatable)."
        ),
        file: str = typer.Option(None, "-f", "--file", help="JSON/YAML request body."),
        output: str = typer.Option(
            None, "-o", "--output", help="Output: table|json|yaml|name|wide."
        ),
    ) -> None:
        """Invoke a custom operation discovered from the OpenAPI spec."""
        sess = _session(ctx)
        if output is not None:
            sess.output = output
        try:
            res = sess.registry.resolve(resource)
        except UnknownResource as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        if action is None or action not in res.custom_ops:
            available = ", ".join(sorted(res.custom_ops)) or "(none)"
            typer.echo(
                f"unknown action for {res.name!r}. Available: {available}", err=True
            )
            raise typer.Exit(1)
        op = res.custom_ops[action]
        params = _parse_params(param or [])
        path = op.path_template
        # Bind the single id-style path param (whatever it is named) from `id`.
        for pname in op.path_params:
            if pname in params:
                path = path.replace(f"{{{pname}}}", str(params.pop(pname)))
            elif id is not None:
                path = path.replace(f"{{{pname}}}", str(id))
        body = _load_body(file)
        try:
            resp = sess.client.request(
                op.method, path, params=params or None, json=body
            )
        except (ApiError, ConnectionFailed) as exc:
            _fail(sess, exc)
            return
        _echo_response(sess, resp)


def _parse_params(pairs: list[str]) -> dict:
    out: dict = {}
    for p in pairs:
        if "=" not in p:
            raise typer.BadParameter(f"--param expects key=value, got {p!r}")
        k, _, v = p.partition("=")
        out[k.strip()] = v
    return out


def _load_body(file: str | None):
    if not file:
        return None
    import yaml
    from pathlib import Path

    return yaml.safe_load(Path(file).read_text())


def _echo_response(sess, resp) -> None:
    if not resp.content:
        typer.echo("ok")
        return
    try:
        data = resp.json()
    except Exception:
        typer.echo(resp.text)
        return
    fmt = sess.output if sess.output not in ("table", "wide") else "yaml"
    typer.echo(render(data, fmt=fmt))
