"""Register the Telegram adapter factory + install PTB handlers."""

from __future__ import annotations

import logging
from typing import Any

from primer.channel.factory import register_adapter_factory
from primer.channel.telegram.adapter import TelegramChannelAdapter
from primer.channel.telegram.connection import TELEGRAM_CONNECTIONS
from primer.channel.telegram.render import build_rejection_prompt
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
)


logger = logging.getLogger(__name__)


_HANDLERS_INSTALLED: set[str] = set()


def _install_handlers(provider_id: str, app: Any) -> None:
    if provider_id in _HANDLERS_INSTALLED:
        return
    _HANDLERS_INSTALLED.add(provider_id)

    from telegram.ext import CallbackQueryHandler, MessageHandler, filters

    async def _on_callback(update, context):
        cq = update.callback_query
        if cq is None:
            return
        await cq.answer()
        chat_id = str(cq.message.chat.id) if cq.message else ""
        entry = TELEGRAM_CONNECTIONS.entry(provider_id)
        if entry is None:
            return
        adapter = entry.adapters_by_chat_id.get(chat_id)
        if adapter is None:
            return
        data = cq.data or ""
        if data.startswith("agentpage:") and getattr(adapter, "_sp", None) is not None:
            try:
                _, chat_id, page_s = data.split(":", 2)
                kb = await adapter.build_agent_picker_keyboard(
                    chat_id=chat_id, page=int(page_s))
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])
                     for b in row] for row in kb])
                await cq.edit_message_reply_markup(reply_markup=markup)
            except Exception:
                logger.exception("telegram: agent-page nav failed")
            return
        if data.startswith("pick_agent:") and getattr(adapter, "_sp", None) is not None:
            notice = await adapter.apply_agent_pick(callback_data=data)
            try:
                await context.bot.send_message(chat_id=cq.message.chat.id, text=notice)
            except Exception:
                logger.exception("telegram: agent-pick notice send failed")
            return
        if (data.startswith("chat_ok:") or data.startswith("chat_no:")) and \
                getattr(adapter, "_sp", None) is not None:
            await adapter.apply_chat_decision_button(callback_data=data)
            return
        if data.startswith("a:"):
            tag = data[2:]
            ids = await adapter._resolve_tag(tag)
            if ids is None:
                return
            await adapter._handle_decision(
                **ids, decision="approved", reason=None,
                telegram_user_id=cq.from_user.id if cq.from_user else None,
            )
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
            sender_name = msg.from_user.full_name if msg.from_user else "user"
            if has_media:
                notice = await adapter.handle_inbound_chat_media(
                    sender_name=sender_name, msg=msg)
            else:
                notice = await adapter.handle_inbound_chat_text(
                    sender_name=sender_name, text=msg.text or "")
            if notice:
                await context.bot.send_message(chat_id=msg.chat.id, text=notice)
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
                await adapter._handle_text_reply(
                    workspace_id=rec.workspace_id,
                    session_id=rec.session_id,
                    tool_call_id=rec.tool_call_id,
                    text=msg.text or "",
                    telegram_user_id=user_id,
                )
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
        if kind == "reject":
            await adapter._handle_decision(
                **ids, decision="rejected", reason=msg.text or "",
                telegram_user_id=user_id,
            )

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
    **_kw,
):
    adapter = TelegramChannelAdapter(
        provider=provider, channel=channel, inbox=inbox,
        storage_provider=storage_provider, event_bus=event_bus,
        claim_engine=claim_engine, artifact_registry=artifact_registry,
    )
    await adapter.initialize()
    conn = TELEGRAM_CONNECTIONS.entry(provider.id)
    if conn is not None:
        _install_handlers(provider.id, conn.app)
    return adapter


register_adapter_factory(ChannelProviderType.TELEGRAM, _telegram_factory)


__all__ = ["_telegram_factory"]
