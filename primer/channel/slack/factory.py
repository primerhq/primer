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


async def _route_channel_event(adapter: Any, provider_id: str, event: dict) -> bool:
    """Normalize a fresh inbound Slack message event and, when a channel-trigger
    rule matches it, fire that rule.

    Returns ``True`` iff a rule matched and was dispatched - in which case the
    caller MUST skip the legacy chat-surface dispatch, so the message is not
    delivered twice (once as a rule action, once as a default chat message).
    Returns ``False`` for correlated replies and unmatched messages, leaving
    the caller's chat dispatch to own delivery.

    The Slack ``event`` dict is already the raw provider payload, so the
    normalizer envelope is just ``{"type": "message", "payload": event}``. Best
    effort: any failure is logged and swallowed (returning ``False``) so it
    never breaks the chat-surface dispatch."""
    router = adapter._inbound_router()
    if router is None:
        return False
    try:
        from primer.channel.slack.normalizer import SlackEventNormalizer

        normalizer = SlackEventNormalizer(provider_id=provider_id)
        normalized = await normalizer.normalize(
            {"type": "message", "payload": event},
        )
        if normalized is None:
            return False
        media_parts = await adapter.collect_inbound_media(event)
        await router.route_event(
            event=normalized, channel=adapter._channel,
            media_parts=media_parts or None,
        )
        return True
    except Exception:  # noqa: BLE001 -- never break chat-surface dispatch
        logger.exception("slack: channel-event routing failed")
        return False


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
            workspace_id=ws, session_id=sid, tool_call_id=tcid,
            decision="approved", reason=None,
            user_id=user_id,
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
                workspace_id=ws, session_id=sid, tool_call_id=tcid,
                decision="rejected", reason=reason,
                user_id=user_id,
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
                        workspace_id=rec.workspace_id, session_id=rec.session_id,
                        tool_call_id=rec.tool_call_id,
                        text=event.get("text", ""),
                        user_id=event.get("user"),
                    )
                    try:
                        await CorrelationStore(sp).clear(
                            adapter._channel.id, thread_ts,
                        )
                    except Exception:
                        pass
                    return
        # Every inbound message is a routed event (S6 section 5): the
        # thread IS the session, so there is no chat dispatch to fall back
        # to and no matcher pre-pass to gate on.
        if getattr(adapter, "_sp", None) is None:
            return
        await _route_channel_event(adapter, provider_id, event)


async def _slack_factory(
    provider: ChannelProvider,
    channel: Channel,
    inbox,
    *,
    storage_provider=None,
    event_bus=None,
    claim_engine=None,
    artifact_registry=None,
    workspace_registry=None,
    scheduler=None,
    **_kw,
):
    adapter = SlackChannelAdapter(
        provider=provider, channel=channel, inbox=inbox,
        storage_provider=storage_provider, event_bus=event_bus,
        claim_engine=claim_engine, artifact_registry=artifact_registry,
        workspace_registry=workspace_registry, scheduler=scheduler,
    )
    await adapter.initialize()
    # The connection is now acquired; install handlers on it once.
    conn = SLACK_CONNECTIONS.entry(provider.id)
    if conn is not None:
        _install_handlers(provider.id, conn.conn.app)
    return adapter


register_adapter_factory(ChannelProviderType.SLACK, _slack_factory)


__all__ = ["_slack_factory"]
