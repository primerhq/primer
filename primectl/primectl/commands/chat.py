"""Chat convenience commands: chat say, chat switch.

These thin commands wrap chat REST operations that do not fit the generic CRUD
shape:

* ``chat say`` appends a user message and wakes the worker over the operator
  message-send endpoint ``POST /v1/chats/{chat_id}/messages`` body
  ``{content}`` -> the appended user_message ChatMessage (202). The reply is
  read back via ``get chat-message`` / the messages GET, not streamed.
* ``chat switch`` re-points a chat at a different agent (auto-rejecting any
  pending gate) over ``POST /v1/chats/{chat_id}/agent`` body ``{agent_id}`` ->
  updated Chat.
"""

from __future__ import annotations

import typer

from primectl.client import ApiError, ConnectionFailed
from primectl.commands.crud import _fail, _session
from primectl.output import render

chat_app = typer.Typer(
    name="chat",
    help="Chat convenience commands (send a message, switch a chat's agent).",
    no_args_is_help=True,
)


@chat_app.command("say")
def say(
    ctx: typer.Context,
    chat_id: str = typer.Argument(..., help="Chat id."),
    message: str = typer.Argument(..., help="The user message text to send."),
    output: str = typer.Option(
        None, "-o", "--output", help="Output: table|json|yaml|name|wide."
    ),
) -> None:
    """Append a user message to a chat and wake the worker.

    Posts to ``POST /v1/chats/{chat_id}/messages`` and prints the appended
    user_message row. The assistant reply is NOT streamed; read it back with
    ``get chat-message`` (after_seq cursor) once the turn drains.
    """
    sess = _session(ctx)
    if output is not None:
        sess.output = output
    try:
        resp = sess.client.request(
            "post",
            f"/v1/chats/{chat_id}/messages",
            json={"content": message},
        )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    if not resp.content:
        typer.echo(f"message sent to chat {chat_id}")
        return
    data = resp.json()
    fmt = sess.output if sess.output not in ("table", "wide") else "yaml"
    typer.echo(render(data, fmt=fmt))


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
