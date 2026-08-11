"""Discovery meta-commands: api-resources, explain."""

from __future__ import annotations

import typer

from primectl.client import ApiError, ConnectionFailed
from primectl.commands.crud import _fail, _session
from primectl.registry import UnknownResource


def register(app: typer.Typer) -> None:
    @app.command(name="api-resources")
    def api_resources(ctx: typer.Context) -> None:
        """List the resources discovered from the server's OpenAPI spec."""
        sess = _session(ctx)
        from rich.console import Console
        from rich.table import Table

        table = Table(show_edge=False, pad_edge=False)
        for col in ("NAME", "ALIASES", "VERBS", "ACTIONS"):
            table.add_column(col)
        for r in sess.registry.all():
            verbs = []
            if r.list_op or r.get_op:
                verbs.append("get")
            if r.create_op:
                verbs.append("create")
            if r.update_op:
                verbs.append("apply")
            if r.delete_op:
                verbs.append("delete")
            table.add_row(
                r.name,
                ",".join(r.aliases),
                ",".join(verbs),
                ",".join(sorted(r.custom_ops)),
            )
        Console().print(table)

    @app.command()
    def capabilities(ctx: typer.Context) -> None:
        """Show which optional subsystems the server has installed."""
        sess = _session(ctx)
        from rich.console import Console
        from rich.table import Table

        try:
            resp = sess.client.request("get", "/v1/capabilities")
        except (ApiError, ConnectionFailed) as exc:
            _fail(sess, exc)
        data = resp.json()
        table = Table(show_edge=False, pad_edge=False)
        for col in ("EXTRA", "INSTALLED", "DETAIL"):
            table.add_column(col)
        for extra, status in sorted(data["extras"].items()):
            detail = ""
            if status.get("platforms"):
                # Only 'channels' carries per-platform detail; list the ones
                # actually importable so a partial install is visible rather
                # than collapsing to a bare "no".
                detail = ",".join(
                    p for p, ok in sorted(status["platforms"].items()) if ok
                ) or "none"
            table.add_row(extra, "yes" if status["installed"] else "no", detail)
        Console().print(table)

    @app.command()
    def explain(ctx: typer.Context, resource: str = typer.Argument(...)) -> None:
        """Show a resource's schema fields (from the spec)."""
        sess = _session(ctx)
        try:
            res = sess.registry.resolve(resource)
        except UnknownResource as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        schema = sess.registry.entity_schema(res)
        if not schema:
            typer.echo(f"{res.name}: no schema available")
            return
        required = set(schema.get("required", []))
        typer.echo(f"{res.name} fields:")
        for fname, fs in schema.get("properties", {}).items():
            ftype = fs.get("type", "any")
            req = " (required)" if fname in required else ""
            typer.echo(f"  {fname}: {ftype}{req}")
