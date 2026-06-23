"""Chat convenience commands: chat switch.

These thin commands wrap chat REST operations that do not fit the generic CRUD
shape. ``chat switch`` re-points a chat at a different agent (auto-rejecting any
pending gate) over the existing operator endpoint:

* ``POST /v1/chats/{chat_id}/agent`` body ``{agent_id}`` -> updated Chat
"""

from __future__ import annotations

import typer

from primectl.client import ApiError, ConnectionFailed
from primectl.commands.crud import _fail, _session
from primectl.output import render

chat_app = typer.Typer(
    name="chat",
    help="Chat convenience commands (switch a chat's agent).",
    no_args_is_help=True,
)


@chat_app.command("switch")
def switch(
    ctx: typer.Context,
    chat_id: str = typer.Argument(..., help="Chat id."),
    agent_id: str = typer.Argument(..., help="Id of the agent to switch the chat to."),
    output: str = typer.Option(
        None, "-o", "--output", help="Output: table|json|yaml|name|wide."
    ),
) -> None:
    """Re-point a chat at a different agent (auto-rejects any pending gate)."""
    sess = _session(ctx)
    if output is not None:
        sess.output = output
    try:
        resp = sess.client.request(
            "post",
            f"/v1/chats/{chat_id}/agent",
            json={"agent_id": agent_id},
        )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    if not resp.content:
        typer.echo(f"chat {chat_id} switched to agent {agent_id}")
        return
    data = resp.json()
    fmt = sess.output if sess.output not in ("table", "wide") else "yaml"
    typer.echo(render(data, fmt=fmt))


def register(app: typer.Typer) -> None:
    app.add_typer(chat_app, name="chat")
