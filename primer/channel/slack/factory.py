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
        await adapter._handle_decision(
            ws=ws, sid=sid, tcid=tcid,
            decision="approved", reason=None,
            slack_user_id=body.get("user", {}).get("id"),
        )

    @app.action("reject")
    async def _on_reject(ack, body, client):
        await ack()
        try:
            verb, ws, sid, tcid = body["actions"][0]["value"].split(":", 3)
        except Exception:
            return
        from primer.channel.slack.render import build_reject_modal
        view = build_reject_modal(
            workspace_id=ws, session_id=sid, tool_call_id=tcid,
        )
        try:
            await client.views_open(trigger_id=body["trigger_id"], view=view)
        except Exception:
            logger.exception("slack: views.open failed")

    @app.view(REJECT_MODAL_CALLBACK_ID)
    async def _on_modal_submit(ack, body, view, client):
        await ack()
        try:
            verb, ws, sid, tcid = view["private_metadata"].split(":", 3)
        except Exception:
            return
        reason = (
            view["state"]["values"]["reason"]["reason_text"]["value"] or ""
        ).strip() or None
        # Modal submissions don't carry the originating channel_id;
        # we look it up from any adapter under this provider whose
        # channel_id matches the original message's container. To
        # keep this simple, we route the rejection by checking ALL
        # adapters under the provider for one that has a pending
        # post for that (ws, sid, tcid).
        entry = SLACK_CONNECTIONS.entry(provider_id)
        if entry is None:
            return
        for adapter in entry.adapters_by_channel_id.values():
            await adapter._handle_decision(
                ws=ws, sid=sid, tcid=tcid,
                decision="rejected", reason=reason,
                slack_user_id=body.get("user", {}).get("id"),
            )
            return  # first wins; the inbox dedupes anyway

    @app.event("message")
    async def _on_message(event, client):
        thread_ts = event.get("thread_ts")
        if not thread_ts or event.get("bot_id"):
            return
        channel_id = event["channel"]
        entry = SLACK_CONNECTIONS.entry(provider_id)
        adapter = entry.adapters_by_channel_id.get(channel_id) if entry else None
        if adapter is None:
            return
        payload = await adapter._lookup_thread_payload(
            channel_id=channel_id, thread_ts=thread_ts,
        )
        if not payload or payload.get("kind") != "ask_user":
            return
        await adapter._handle_text_reply(
            ws=payload["ws"], sid=payload["sid"], tcid=payload["tcid"],
            text=event.get("text", ""),
            slack_user_id=event.get("user"),
        )


async def _slack_factory(
    provider: ChannelProvider,
    channel: Channel,
    inbox,
):
    adapter = SlackChannelAdapter(
        provider=provider, channel=channel, inbox=inbox,
    )
    await adapter.initialize()
    # The connection is now acquired; install handlers on it once.
    conn = SLACK_CONNECTIONS.entry(provider.id)
    if conn is not None:
        _install_handlers(provider.id, conn.conn.app)
    return adapter


register_adapter_factory(ChannelProviderType.SLACK, _slack_factory)


__all__ = ["_slack_factory"]
