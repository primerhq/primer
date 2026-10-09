"""Register the Telegram adapter factory + install PTB handlers."""

from __future__ import annotations

import logging
from typing import Any

from primer.channel.adapter import BUTTON_EXPIRED_NOTICE, refusal_notice
from primer.channel.factory import register_adapter_factory
from primer.channel.telegram.adapter import TelegramChannelAdapter
from primer.channel.telegram.connection import TELEGRAM_CONNECTIONS
from primer.channel.telegram.render import build_rejection_prompt
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
)


logger = logging.getLogger(__name__)


_HANDLERS_INSTALLED: set[str] = set()


async def _route_channel_event(adapter: Any, provider_id: str, msg: Any) -> bool:
    """Normalize a fresh inbound PTB message and, when a channel-trigger rule
    matches it, fire that rule.

    Returns ``True`` iff a rule matched and was dispatched - in which case the
    caller MUST skip the legacy chat-surface dispatch, so the message is not
    delivered twice (once as a rule action, once as a default chat message).
    Returns ``False`` for correlated replies and unmatched messages, leaving
    the caller's chat dispatch to own delivery.

    Builds the small dict envelope ``{"type": "message", "payload": {...}}`` the
    provider :class:`TelegramEventNormalizer` consumes (so the normalizer stays
    SDK-free), normalizes it, and - when a :class:`ChannelEvent` results - hands
    it to the adapter's event router. A best-effort path: any failure is logged
    and swallowed (returning ``False``) so it never breaks the chat-surface
    dispatch."""
    router = adapter._inbound_router()
    if router is None:
        return False
    try:
        from primer.channel.telegram.normalizer import TelegramEventNormalizer

        chat = msg.chat
        sender = msg.from_user
        entities = [
            {
                "type": getattr(e, "type", None),
                "offset": getattr(e, "offset", 0),
                "length": getattr(e, "length", 0),
            }
            for e in (msg.entities or [])
        ]
        payload = {
            "message_id": msg.message_id,
            "chat": {
                "id": getattr(chat, "id", None),
                "type": getattr(chat, "type", None),
            },
            "from": {
                "id": getattr(sender, "id", None) if sender else None,
                "full_name": getattr(sender, "full_name", None) if sender else None,
            },
            "text": msg.text or msg.caption or "",
            "entities": entities,
        }
        normalizer = TelegramEventNormalizer(provider_id=provider_id)
        event = await normalizer.normalize({"type": "message", "payload": payload})
        if event is None:
            return False
        media_parts = await adapter.collect_inbound_media(msg)
        await router.route_event(
            event=event, channel=adapter._channel,
            media_parts=media_parts or None,
        )
        return True
    except Exception:  # noqa: BLE001 -- never break chat-surface dispatch
        logger.exception("telegram: channel-event routing failed")
        return False


def _install_handlers(provider_id: str, app: Any) -> None:
    if provider_id in _HANDLERS_INSTALLED:
        return
    _HANDLERS_INSTALLED.add(provider_id)

    from telegram.ext import CallbackQueryHandler, MessageHandler, filters

    async def _on_callback(update, context):
        cq = update.callback_query
        if cq is None:
            return
        # The click is answered ONCE, on the way out whatever the branch: a refused approval answers it with an alert only the clicker
        # sees (the routing notice), anything else with the plain acknowledgement that stops the button's spinner.
        notice: str | None = None
        try:
            chat_id = str(cq.message.chat.id) if cq.message else ""
            entry = TELEGRAM_CONNECTIONS.entry(provider_id)
            if entry is None:
                return
            adapter = entry.adapters_by_chat_id.get(chat_id)
            if adapter is None:
                return
            data = cq.data or ""
            if data.startswith("a:"):
                tag = data[2:]
                ids = await adapter._resolve_tag(tag)
                if ids is None:
                    # The cache no longer knows this button (the process restarted, or the entry aged out): nothing is decided, and silence would
                    # leave the person guessing. Only the clicker sees the alert.
                    notice = BUTTON_EXPIRED_NOTICE
                    return
                accepted = await adapter._handle_decision(
                    **ids, decision="approved", reason=None,
                    user_id=cq.from_user.id if cq.from_user else None,
                )
                if not accepted:
                    # Refused: the gate is routed to specific approvers, or the click is for an approval since replaced. The message is not
                    # marked approved, and the clicker is told which.
                    notice = refusal_notice(accepted)
                    return
                try:
                    await context.bot.edit_message_text(
                        chat_id=cq.message.chat.id,
                        message_id=cq.message.message_id,
                        text=f"{cq.message.text}\n\n✓ Approved",
                    )
                except Exception:
                    logger.exception("telegram: edit_message_text failed")
            elif data.startswith("r:"):
                tag = data[2:]
                ids = await adapter._resolve_tag(tag)
                if ids is None:
                    notice = BUTTON_EXPIRED_NOTICE
                    return
                sent = await context.bot.send_message(
                    chat_id=cq.message.chat.id, **build_rejection_prompt(),
                )
                # The reason arrives as a reply to this prompt; correlate by id.
                mid = getattr(sent, "message_id", 0)
                if mid:
                    adapter.remember_reply_target(
                        message_id=mid, ids=ids, kind="reject",
                    )
        finally:
            # Telegram refuses to answer a click that is too old (and the chat can be gone): that must not turn an accepted decision, or the
            # exception already on its way out, into a different error.
            try:
                if notice is not None:
                    await cq.answer(text=notice, show_alert=True)
                else:
                    await cq.answer()
            except Exception:
                logger.exception("telegram: could not answer the callback query")

    async def _on_message(update, context):
        msg = update.message
        if msg is None:
            return
        chat_id = str(msg.chat.id)
        entry = TELEGRAM_CONNECTIONS.entry(provider_id)
        if entry is None:
            return
        adapter = entry.adapters_by_chat_id.get(chat_id)
        if adapter is None:
            return
        # Chat-surface dispatch: a non-reply message on a chat-enabled adapter
        # is a chat turn or a /command. Reply messages (and adapters without a
        # storage_provider) fall through to the session gate-reply path below.
        has_media = any((
            msg.photo, msg.document, msg.audio, msg.voice, msg.video,
        ))
        if not msg.reply_to_message and getattr(adapter, "_sp", None) is not None:
            # Every inbound message is a routed event (S6 section 5).
            await _route_channel_event(adapter, provider_id, msg)
            return
        if not msg.reply_to_message:
            return
        replied_mid = msg.reply_to_message.message_id
        user_id = msg.from_user.id if msg.from_user else None
        # Session ask_user: try the persistent store first (survives restarts),
        # then fall back to the in-memory _reply_targets cache.
        sp = getattr(adapter, "_sp", None)
        if sp is not None:
            from primer.channel.correlation import CorrelationStore
            try:
                rec = await CorrelationStore(sp).lookup(
                    adapter._channel.id, str(replied_mid),
                )
            except Exception:
                rec = None
            if rec is not None and rec.kind == "session":
                relayed = await adapter._handle_text_reply(
                    workspace_id=rec.workspace_id,
                    session_id=rec.session_id,
                    tool_call_id=rec.tool_call_id,
                    text=msg.text or "",
                    user_id=user_id,
                    gate_id=getattr(rec, "gate_id", None),
                )
                if not relayed:
                    # The question was replaced since this message: the reply answered nothing. Tell the person (best effort).
                    try:
                        await context.bot.send_message(
                            chat_id=msg.chat.id, text=refusal_notice(relayed), reply_to_message_id=msg.message_id,
                        )
                    except Exception:
                        logger.exception("telegram: could not send the stale-question notice")
                try:
                    await CorrelationStore(sp).clear(
                        adapter._channel.id, str(replied_mid),
                    )
                except Exception:
                    pass
                # Remove from in-memory cache if it was also stored there.
                adapter._reply_targets.pop(replied_mid, None)
                return
        # Fallback: in-memory cache for the tool-rejection reason path.
        # (The Reject button sends a follow-up text prompt; that reply target
        # is stored in _reply_targets with kind="reject".)
        target = adapter.resolve_reply_target(replied_mid)
        if target is None:
            return
        kind = target.get("kind")
        ids = {k: target[k] for k in ("workspace_id", "session_id", "tool_call_id")}
        if target.get("gate_id"):
            ids["gate_id"] = target["gate_id"]          # the gate the Reject button belonged to (C-033)
        if kind == "reject":
            accepted = await adapter._handle_decision(
                **ids, decision="rejected", reason=msg.text or "",
                user_id=user_id,
            )
            if not accepted:
                # Refused: the gate is routed to specific approvers. Reply to the reason, so the clicker is told (best effort: the
                # decision was judged either way, and a chat that cannot be written to must not fail the handler).
                try:
                    await context.bot.send_message(
                        chat_id=msg.chat.id, text=refusal_notice(accepted), reply_to_message_id=msg.message_id,
                    )
                except Exception:
                    logger.exception("telegram: could not send the approval-routed notice")

    app.add_handler(CallbackQueryHandler(_on_callback))
    # Text plus inbound media (photo/document/audio/voice/video). The caption
    # carries the user text on media messages.
    media_filter = (
        filters.TEXT
        | filters.PHOTO
        | filters.Document.ALL
        | filters.AUDIO
        | filters.VOICE
        | filters.VIDEO
    )
    app.add_handler(MessageHandler(media_filter, _on_message))


async def _telegram_factory(
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
    adapter = TelegramChannelAdapter(
        provider=provider, channel=channel, inbox=inbox,
        storage_provider=storage_provider, event_bus=event_bus,
        claim_engine=claim_engine, artifact_registry=artifact_registry,
        workspace_registry=workspace_registry, scheduler=scheduler,
    )
    await adapter.initialize()
    conn = TELEGRAM_CONNECTIONS.entry(provider.id)
    if conn is not None:
        _install_handlers(provider.id, conn.app)
    return adapter


register_adapter_factory(ChannelProviderType.TELEGRAM, _telegram_factory)


__all__ = ["_telegram_factory"]
