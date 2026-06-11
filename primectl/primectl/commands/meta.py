"""Discovery meta-commands: api-resources, explain."""

from __future__ import annotations

import typer

from primectl.commands.crud import _session
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
