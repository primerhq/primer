"""Channel reply-binding and channel-trigger/subscription convenience commands."""

from __future__ import annotations

import typer

from primectl.client import ApiError, ConnectionFailed
from primectl.commands.crud import _assemble_set, _fail, _session
from primectl.output import render

channel_app = typer.Typer(
    name="channel",
    help="Channel reply-binding and channel-trigger convenience commands.",
    no_args_is_help=True,
)

binding_app = typer.Typer(
    name="binding",
    help="Manage a workspace reply_binding (the channel its replies are sent to).",
    no_args_is_help=True,
)


@binding_app.command("set")
def binding_set(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(..., help="Workspace id."),
    channel_id: str = typer.Argument(..., help="Channel id to bind replies to."),
) -> None:
    """Bind a workspace reply_binding to a channel."""
    sess = _session(ctx)
    try:
        resp = sess.client.request(
            "put",
            f"/v1/workspaces/{workspace_id}/reply_binding",
            json={"channel_id": channel_id},
        )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    _echo(sess, resp)


@binding_app.command("clear")
def binding_clear(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(..., help="Workspace id."),
) -> None:
    """Clear a workspace reply_binding."""
    sess = _session(ctx)
    try:
        resp = sess.client.request(
            "delete", f"/v1/workspaces/{workspace_id}/reply_binding"
        )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    _echo(sess, resp)


@binding_app.command("get")
def binding_get(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(..., help="Workspace id."),
) -> None:
    """Show a workspace reply_binding."""
    sess = _session(ctx)
    try:
        resp = sess.client.request("get", f"/v1/workspaces/{workspace_id}")
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    data = resp.json() if resp.content else {}
    typer.echo(render(data.get("reply_binding"), fmt="yaml"))


trigger_app = typer.Typer(
    name="trigger",
    help="Create channel triggers without hand-writing the discriminated config.",
    no_args_is_help=True,
)


@trigger_app.command("create")
def trigger_create(
    ctx: typer.Context,
    provider: str = typer.Option(..., "--provider", help="Channel provider id."),
    slug: str = typer.Option(..., "--slug", help="Trigger slug."),
    name: str = typer.Option(..., "--name", help="Trigger display name."),
    channel: str = typer.Option(
        None, "--channel", help="Channel id (omit for provider-wide)."
    ),
) -> None:
    """Create a channel trigger (POST /v1/triggers)."""
    sess = _session(ctx)
    config: dict = {"kind": "channel", "provider_id": provider}
    if channel:
        config["channel_id"] = channel
    body = {"slug": slug, "name": name, "config": config}
    try:
        resp = sess.client.request("post", "/v1/triggers", json=body)
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    _echo(sess, resp)


sub_app = typer.Typer(
    name="sub",
    help="Create channel-trigger subscriptions (event -> action bindings).",
    no_args_is_help=True,
)


@sub_app.command("create")
def sub_create(
    ctx: typer.Context,
    trigger_id: str = typer.Argument(..., help="Trigger id."),
    action: str = typer.Option(
        ..., "--action", help="Action kind (the subscription config kind)."
    ),
    event_type: str = typer.Option(
        ..., "--event-type", help="Channel event type to match."
    ),
    command: str = typer.Option(
        None, "--command", help="Match a specific command name."
    ),
    mentions_bot: bool = typer.Option(
        None, "--mentions-bot/--no-mentions-bot", help="Match bot-mention state."
    ),
    surface: str = typer.Option(None, "--surface", help="Match a channel surface."),
    text_pattern: str = typer.Option(
        None, "--text-pattern", help="Match a message text pattern."
    ),
    reply_target: str = typer.Option(
        None, "--reply-target", help="Reply target for produced replies."
    ),
    set_: list[str] = typer.Option(
        None, "--set", help="Action config field=value (repeatable)."
    ),
) -> None:
    """Create a subscription binding an event matcher to an action."""
    sess = _session(ctx)
    config: dict = {"kind": action, **_assemble_set(set_ or [])}
    event_matcher: dict = {"event_type": event_type}
    if command is not None:
        event_matcher["command_name"] = command
    if mentions_bot is not None:
        event_matcher["mentions_bot"] = mentions_bot
    if surface is not None:
        event_matcher["surface"] = surface
    if text_pattern is not None:
        event_matcher["text_pattern"] = text_pattern
    body: dict = {"config": config, "event_matcher": event_matcher}
    if reply_target is not None:
        body["reply_target"] = reply_target
    try:
        resp = sess.client.request(
            "post", f"/v1/triggers/{trigger_id}/subscriptions", json=body
        )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    _echo(sess, resp)


def _echo(sess, resp) -> None:
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


def register(app: typer.Typer) -> None:
    channel_app.add_typer(binding_app, name="binding")
    channel_app.add_typer(trigger_app, name="trigger")
    channel_app.add_typer(sub_app, name="sub")
    app.add_typer(channel_app, name="channel")
