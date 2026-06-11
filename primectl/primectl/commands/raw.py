"""Raw request escape hatch: primectl raw <METHOD> <path>."""

from __future__ import annotations

import typer

from primectl.client import ApiError, ConnectionFailed
from primectl.commands.call import _load_body, _parse_params, _echo_response
from primectl.commands.crud import _fail, _session


def register(app: typer.Typer) -> None:
    @app.command()
    def raw(
        ctx: typer.Context,
        method: str = typer.Argument(..., help="HTTP method, e.g. GET."),
        path: str = typer.Argument(..., help="Path, e.g. /v1/health."),
        param: list[str] = typer.Option(None, "--param", help="query key=value."),
        file: str = typer.Option(None, "-f", "--file", help="JSON/YAML body."),
        output: str = typer.Option(
            None, "-o", "--output", help="Output: table|json|yaml|name|wide."
        ),
    ) -> None:
        """Issue a raw request against the server (ultimate escape hatch)."""
        sess = _session(ctx)
        if output is not None:
            sess.output = output
        params = _parse_params(param or [])
        body = _load_body(file)
        try:
            resp = sess.client.request(
                method.lower(), path, params=params or None, json=body
            )
        except (ApiError, ConnectionFailed) as exc:
            _fail(sess, exc)
            return
        _echo_response(sess, resp)
