"""Register the Slack adapter factory at import time.

Imported once from ``primer/api/app.py`` (or wherever boot-time
factory registration happens). The module body runs the
``register_adapter_factory`` call so the channels core can
build SlackChannelAdapter instances when asked.

ALSO registers the bolt action / view / message handlers on each
shared connection at first-use. Re-registration is idempotent
because slack_bolt's app.action() uses a (constraint -> handler)
map; the connection registry never registers twice for the same
connection.
"""

from __future__ import annotations

import logging
from typing import Any

from primer.channel.factory import register_adapter_factory
from primer.channel.slack.adapter import (
    REJECT_MODAL_CALLBACK_ID,
    SlackChannelAdapter,
)
from primer.channel.slack.connection import SLACK_CONNECTIONS
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
)


logger = logging.getLogger(__name__)


_HANDLERS_INSTALLED: set[str] = set()


def _install_handlers(provider_id: str, app: Any) -> None:
    """One-shot handler installation per shared connection."""
    if provider_id in _HANDLERS_INSTALLED:
        return
    _HANDLERS_INSTALLED.add(provider_id)

    @app.action("approve")
    async def _on_approve(ack, body, client):
        await ack()
        try:
            verb, ws, sid, tcid = body["actions"][0]["value"].split(":", 3)
        except Exception:
            logger.warning("slack: malformed approve value")
            return
        channel_id = body["channel"]["id"]
        entry = SLACK_CONNECTIONS.entry(provider_id)
        adapter = entry.adapters_by_channel_id.get(channel_id) if entry else None
        if adapter is None:
            return
        user_id = body.get("user", {}).get("id")
        await adapter._handle_decision(
            ws=ws, sid=sid, tcid=tcid,
            decision="approved", reason=None,
            slack_user_id=user_id,
        )
        # Replace the buttons with an "Approved by @user" note.
        from primer.channel.slack.render import build_decided_blocks
        msg = body.get("message", {})
        try:
            await client.chat_update(
                channel=channel_id, ts=msg.get("ts"),
                blocks=build_decided_blocks(
                    original_blocks=msg.get("blocks"),
                    decision="approved", slack_user_id=user_id,
                ),
                text="Tool call approved",
            )
        except Exception:
            logger.exception("slack: chat.update after approve failed")

    @app.action("reject")
    async def _on_reject(ack, body, client):
        await ack()
        try:
            verb, ws, sid, tcid = body["actions"][0]["value"].split(":", 3)
        except Exception:
            return
        from primer.channel.slack.render import build_reject_modal
        # Carry the originating channel + message ts so the modal-submit
        # handler can update the original message after the reason is given.
        view = build_reject_modal(
            workspace_id=ws, session_id=sid, tool_call_id=tcid,
            channel_id=body.get("channel", {}).get("id"),
            message_ts=body.get("message", {}).get("ts"),
        )
        try:
            await client.views_open(trigger_id=body["trigger_id"], view=view)
        except Exception:
            logger.exception("slack: views.open failed")

    @app.action("pick_agent")
    async def _on_pick_agent(ack, body, client):
        await ack()
        try:
            value = body["actions"][0]["selected_option"]["value"]
        except Exception:
            logger.warning("slack: malformed pick_agent payload")
            return
        channel_id = body.get("channel", {}).get("id")
        entry = SLACK_CONNECTIONS.entry(provider_id)
        adapter = entry.adapters_by_channel_id.get(channel_id) if entry else None
        if adapter is None or getattr(adapter, "_sp", None) is None:
            return
        from primer.channel.slack.blocks import parse_agent_selection
        try:
            notice = await parse_agent_selection(
                storage_provider=adapter._sp, selected_value=value)
        except Exception:
            logger.exception("slack: pick_agent set_agent failed")
            return
        try:
            thread_ts = body.get("message", {}).get("thread_ts")
            await client.chat_postMessage(
                channel=channel_id, text=notice,
                **({"thread_ts": thread_ts} if thread_ts else {}),
            )
        except Exception:
            logger.exception("slack: pick_agent post notice failed")

    @app.view(REJECT_MODAL_CALLBACK_ID)
    async def _on_modal_submit(ack, body, view, client):
        await ack()
        # private_metadata: reject:ws:sid:tcid[:channel:ts]
        parts = view.get("private_metadata", "").split(":")
        if len(parts) < 4:
            return
        ws, sid, tcid = parts[1], parts[2], parts[3]
        channel_id = parts[4] if len(parts) > 4 and parts[4] else None
        message_ts = parts[5] if len(parts) > 5 and parts[5] else None
        reason = (
            view["state"]["values"]["reason"]["reason_text"]["value"] or ""
        ).strip() or None
        user_id = body.get("user", {}).get("id")
        # Modal submissions don't carry the originating channel_id; route the
        # rejection through any adapter under this provider (inbox dedupes).
        entry = SLACK_CONNECTIONS.entry(provider_id)
        if entry is None:
            return
        for adapter in entry.adapters_by_channel_id.values():
            await adapter._handle_decision(
                ws=ws, sid=sid, tcid=tcid,
                decision="rejected", reason=reason,
                slack_user_id=user_id,
            )
            break  # first wins; the inbox dedupes anyway
        # Replace the buttons on the original message with a "Rejected" note.
        if channel_id and message_ts:
            from primer.channel.slack.render import build_decided_blocks
            orig_blocks = None
            try:
                hist = await client.conversations_history(
                    channel=channel_id, latest=message_ts,
                    oldest=message_ts, inclusive=True, limit=1,
                )
                orig_blocks = (hist.get("messages") or [{}])[0].get("blocks")
            except Exception:
                logger.warning("slack: history lookup for reject update failed")
            try:
                await client.chat_update(
                    channel=channel_id, ts=message_ts,
                    blocks=build_decided_blocks(
                        original_blocks=orig_blocks, decision="rejected",
                        slack_user_id=user_id, reason=reason,
                    ),
                    text="Tool call rejected",
                )
            except Exception:
                logger.exception("slack: chat.update after reject failed")

    async def _run_slash(command, body, client) -> None:
        """Shared body for the /new, /list, /agent slash commands.

        Slack delivers slash commands at the channel level (no thread_ts in
        the payload), so chat targeting falls back to the channel. Posts a
        plain-text rendering of the CommandResult; a Block Kit picker can
        replace the agent list once Task 20's blocks module exists.
        """
        from primer.channel.slack.commands import handle_slash_command
        channel_id = body.get("channel_id")
        entry = SLACK_CONNECTIONS.entry(provider_id)
        adapter = (
            entry.adapters_by_channel_id.get(channel_id) if entry else None
        )
        if adapter is None or getattr(adapter, "_sp", None) is None:
            return
        try:
            res = await handle_slash_command(
                storage_provider=adapter._sp,
                command=command,
                text=(body.get("text") or "").strip(),
                channel_id=adapter._channel.id,
                thread_ts=None,
            )
        except Exception:
            logger.exception("slack: slash command %s failed", command)
            return
        if res.kind == "list":
            if res.items:
                lines = [
                    f"- {it['title']} ({it['chat_id']}) -> {it['agent_id']}"
                    for it in res.items
                ]
                text = "Chats on this channel:\n" + "\n".join(lines)
            else:
                text = "No chats yet on this channel."
        elif res.kind == "chat_picker":
            # Native /agent has no thread context -> render a paginated chat
            # picker; choosing a chat opens its agent select.
            if not res.items:
                text = "No chats yet on this channel. Post a message to start one."
            else:
                from primer.channel.slack.blocks import build_chat_select_blocks
                try:
                    await client.chat_postMessage(
                        channel=channel_id,
                        blocks=build_chat_select_blocks(res.items, page=0),
                        text="Pick a chat to switch its agent:",
                    )
                except Exception:
                    logger.exception("slack: posting chat picker failed")
                return
        else:
            text = res.text or ""
        if not text:
            return
        try:
            await client.chat_postMessage(channel=channel_id, text=text)
        except Exception:
            logger.exception("slack: posting slash result for %s failed", command)

    async def _adapter_for_body(body):
        cid = body.get("channel", {}).get("id")
        entry = SLACK_CONNECTIONS.entry(provider_id)
        adapter = entry.adapters_by_channel_id.get(cid) if entry else None
        if adapter is None or getattr(adapter, "_sp", None) is None:
            return None
        return adapter

    async def _on_chat_page(ack, body, client):
        """Re-render the paginated chat picker for another page (in place)."""
        await ack()
        adapter = await _adapter_for_body(body)
        if adapter is None:
            return
        try:
            page = int(body["actions"][0]["value"])
        except Exception:
            return
        from primer.channel.commands import CommandExecutor
        from primer.channel.slack.blocks import build_chat_select_blocks
        res = await CommandExecutor(storage_provider=adapter._sp).list_chats(
            channel_id=adapter._channel.id)
        msg = body.get("message", {})
        try:
            await client.chat_update(
                channel=body["channel"]["id"], ts=msg.get("ts"),
                blocks=build_chat_select_blocks(res.items, page=page),
                text="Pick a chat to switch its agent:")
        except Exception:
            logger.exception("slack: chat_page update failed")

    @app.action("chat_page_prev")
    async def _on_chat_page_prev(ack, body, client):
        await _on_chat_page(ack, body, client)

    @app.action("chat_page_next")
    async def _on_chat_page_next(ack, body, client):
        await _on_chat_page(ack, body, client)

    @app.action("pick_chat_agent")
    async def _on_pick_chat_agent(ack, body, client):
        """A chat was chosen from the paginated picker -> show its agent select."""
        await ack()
        adapter = await _adapter_for_body(body)
        if adapter is None:
            return
        try:
            chat_id = body["actions"][0]["selected_option"]["value"]
        except Exception:
            logger.warning("slack: malformed pick_chat_agent payload")
            return
        from primer.channel.commands import CommandExecutor
        from primer.channel.slack.blocks import build_agent_select_blocks
        picker = await CommandExecutor(storage_provider=adapter._sp).agent_picker(
            channel_id=adapter._channel.id)
        if not picker.items:
            return
        msg = body.get("message", {})
        try:
            await client.chat_update(
                channel=body["channel"]["id"], ts=msg.get("ts"),
                blocks=build_agent_select_blocks(result=picker, chat_id=chat_id),
                text="Pick an agent:")
        except Exception:
            logger.exception("slack: pick_chat_agent update failed")

    # No /new or /list on Slack: a new thread is a new chat, and the channel's
    # threads are the chat list.
    @app.command("/agent")
    async def _on_agent(ack, body, client):
        await ack()
        await _run_slash("/agent", body, client)

    @app.command("/help")
    async def _on_help(ack, body, client):
        await ack()
        await _run_slash("/help", body, client)

    @app.event("message")
    async def _on_message(event, client):
        # Ignore bot/self messages and edits/deletes (no plain text payload).
        # A "file_share" subtype carries the user's uploaded files, so it is
        # let through (any other subtype - edits, deletes, joins - is dropped).
        subtype = event.get("subtype")
        if event.get("bot_id") or (subtype and subtype != "file_share"):
            return
        thread_ts = event.get("thread_ts")
        channel_id = event["channel"]
        entry = SLACK_CONNECTIONS.entry(provider_id)
        adapter = entry.adapters_by_channel_id.get(channel_id) if entry else None
        if adapter is None:
            return
        # Session-prompt reply: a reply in a session thread carries
        # thread_ts = the thread root ts that an ask_user is parked on. The
        # store is the authoritative source; the path takes precedence over
        # chat-surface dispatch so existing session gates keep working.
        if thread_ts is not None:
            sp = getattr(adapter, "_sp", None)
            if sp is not None:
                from primer.channel.correlation import CorrelationStore
                try:
                    rec = await CorrelationStore(sp).lookup(
                        adapter._channel.id, thread_ts,
                    )
                except Exception:
                    rec = None
                if rec is not None and rec.kind == "session":
                    await adapter._handle_text_reply(
                        ws=rec.workspace_id, sid=rec.session_id,
                        tcid=rec.tool_call_id,
                        text=event.get("text", ""),
                        slack_user_id=event.get("user"),
                    )
                    try:
                        await CorrelationStore(sp).clear(
                            adapter._channel.id, thread_ts,
                        )
                    except Exception:
                        pass
                    return
        # Chat-surface dispatch: on a chat-enabled adapter, a top-level message
        # opens a new thread-chat and an in-thread message routes to its chat.
        if getattr(adapter, "_sp", None) is None:
            return
        sender_name = event.get("user") or "user"
        await adapter.handle_inbound_chat_message(
            thread_ts=thread_ts, message_ts=event.get("ts", ""),
            sender_name=sender_name, text=event.get("text", ""),
            files=event.get("files"),
        )


async def _slack_factory(
    provider: ChannelProvider,
    channel: Channel,
    inbox,
    *,
    storage_provider=None,
    event_bus=None,
    claim_engine=None,
    artifact_registry=None,
    **_kw,
):
    adapter = SlackChannelAdapter(
        provider=provider, channel=channel, inbox=inbox,
        storage_provider=storage_provider, event_bus=event_bus,
        claim_engine=claim_engine, artifact_registry=artifact_registry,
    )
    await adapter.initialize()
    # The connection is now acquired; install handlers on it once.
    conn = SLACK_CONNECTIONS.entry(provider.id)
    if conn is not None:
        _install_handlers(provider.id, conn.conn.app)
    return adapter


register_adapter_factory(ChannelProviderType.SLACK, _slack_factory)


__all__ = ["_slack_factory"]
