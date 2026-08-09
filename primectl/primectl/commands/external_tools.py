"""External (invoker-supplied) tool call commands.

Discovery + scripted responding for the external-tools surface:

* ``external-tools list`` reads the global audit/poll endpoint
  ``GET /v1/external_tool_calls`` (filters: status / session / chat).
* ``external-tools pending`` reads the per-conversation pending list
  ``GET /v1/{sessions|chats}/{id}/external_tools/pending``.
* ``external-tools respond`` feeds a result back through the SAME
  invocation endpoint the owning surface uses: session parks resolve
  via ``POST /v1/workspaces/{wid}/sessions/{sid}/steer`` (the
  workspace id is resolved from the session row), chat pendings via
  ``POST /v1/chats/{id}/messages`` - both with a ``tool_results``
  body. There is no separate respond API.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from primectl.client import ApiError, ConnectionFailed
from primectl.commands.crud import _fail, _session
from primectl.output import render

external_tools_app = typer.Typer(
    name="external-tools",
    help="Invoker-supplied tool calls: list, pending, respond.",
    no_args_is_help=True,
)


def _parse_result(value: str):
    """Parse ``--result``: ``@path`` reads a JSON file; otherwise the
    value itself is parsed as JSON, falling back to the raw string."""
    if value.startswith("@"):
        value = Path(value[1:]).read_text(encoding="utf-8")
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _echo(sess, data, output: str | None) -> None:
    if output is not None:
        sess.output = output
    fmt = sess.output if sess.output not in ("table", "wide") else "yaml"
    typer.echo(render(data, fmt=fmt))


@external_tools_app.command("list")
def list_calls(
    ctx: typer.Context,
    status: str = typer.Option(
        None, "--status", help="Filter: pending|completed|cancelled|timed_out."
    ),
    session: str = typer.Option(None, "--session", help="Filter by session id."),
    chat: str = typer.Option(None, "--chat", help="Filter by chat id."),
    output: str = typer.Option(
        None, "-o", "--output", help="Output: table|json|yaml|name|wide."
    ),
) -> None:
    """List external tool calls across every conversation (audit/poll)."""
    sess = _session(ctx)
    params = {
        k: v
        for k, v in (
            ("status", status),
            ("session_id", session),
            ("chat_id", chat),
        )
        if v
    }
    try:
        resp = sess.client.request(
            "get", "/v1/external_tool_calls", params=params
        )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    _echo(sess, resp.json(), output)


@external_tools_app.command("pending")
def pending(
    ctx: typer.Context,
    session: str = typer.Option(None, "--session", help="Session id."),
    chat: str = typer.Option(None, "--chat", help="Chat id."),
    output: str = typer.Option(
        None, "-o", "--output", help="Output: table|json|yaml|name|wide."
    ),
) -> None:
    """List one conversation's pending external tool calls."""
    if not (session or chat):
        raise typer.BadParameter("pass --session or --chat")
    sess = _session(ctx)
    base = f"/v1/sessions/{session}" if session else f"/v1/chats/{chat}"
    try:
        resp = sess.client.request("get", f"{base}/external_tools/pending")
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    _echo(sess, resp.json(), output)


@external_tools_app.command("respond")
def respond(
    ctx: typer.Context,
    tool_call_id: str = typer.Argument(..., help="The pending tool_call_id."),
    session: str = typer.Option(None, "--session", help="Owning session id."),
    chat: str = typer.Option(None, "--chat", help="Owning chat id."),
    result: str = typer.Option(
        ..., "--result", help="Result JSON (inline, or @path to a file)."
    ),
    error: bool = typer.Option(
        False, "--error", help="Flag the result as a tool-level error."
    ),
    output: str = typer.Option(
        None, "-o", "--output", help="Output: table|json|yaml|name|wide."
    ),
) -> None:
    """Resolve a pending external tool call through the invocation API."""
    if bool(session) == bool(chat):
        raise typer.BadParameter("pass exactly one of --session / --chat")
    sess = _session(ctx)
    body = {
        "tool_results": [
            {
                "tool_call_id": tool_call_id,
                "result": _parse_result(result),
                "is_error": error,
            }
        ]
    }
    try:
        if session:
            row = sess.client.request(
                "get", f"/v1/sessions/{session}"
            ).json()
            wid = row.get("workspace_id")
            resp = sess.client.request(
                "post",
                f"/v1/workspaces/{wid}/sessions/{session}/steer",
                json=body,
            )
        else:
            resp = sess.client.request(
                "post", f"/v1/chats/{chat}/messages", json=body
            )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    if not resp.content:
        typer.echo(f"tool result accepted for {tool_call_id}")
        return
    _echo(sess, resp.json(), output)


def register(app: typer.Typer) -> None:
    app.add_typer(external_tools_app, name="external-tools")
