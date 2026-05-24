"""Register the Telegram adapter factory + install PTB handlers."""

from __future__ import annotations

import logging
from typing import Any

from matrix.channel.factory import register_adapter_factory
from matrix.channel.telegram.adapter import TelegramChannelAdapter
from matrix.channel.telegram.connection import TELEGRAM_CONNECTIONS
from matrix.channel.telegram.render import (
    ASK_TOKEN_RE, REJECT_TOKEN_RE,
    build_rejection_prompt,
)
from matrix.model.channel import (
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
            body = build_rejection_prompt(tag=tag)
            await context.bot.send_message(
                chat_id=cq.message.chat.id, **body,
            )

    async def _on_message(update, context):
        msg = update.message
        if msg is None or not msg.reply_to_message or not msg.reply_to_message.text:
            return
        parent = msg.reply_to_message.text
        chat_id = str(msg.chat.id)
        entry = TELEGRAM_CONNECTIONS.entry(provider_id)
        if entry is None:
            return
        adapter = entry.adapters_by_chat_id.get(chat_id)
        if adapter is None:
            return
        rej = REJECT_TOKEN_RE.search(parent)
        if rej:
            tag = rej.group(1)
            ids = await adapter._resolve_tag(tag)
            if ids is None:
                return
            await adapter._handle_decision(
                **ids, decision="rejected",
                reason=msg.text or "",
                telegram_user_id=msg.from_user.id if msg.from_user else None,
            )
            return
        ask = ASK_TOKEN_RE.search(parent)
        if ask:
            tag = ask.group(1)
            ids = await adapter._resolve_tag(tag)
            if ids is None:
                return
            await adapter._handle_text_reply(
                **ids, text=msg.text or "",
                telegram_user_id=msg.from_user.id if msg.from_user else None,
            )

    app.add_handler(CallbackQueryHandler(_on_callback))
    app.add_handler(MessageHandler(filters.REPLY & ~filters.COMMAND, _on_message))


async def _telegram_factory(
    provider: ChannelProvider,
    channel: Channel,
    inbox,
):
    adapter = TelegramChannelAdapter(
        provider=provider, channel=channel, inbox=inbox,
    )
    await adapter.initialize()
    conn = TELEGRAM_CONNECTIONS.entry(provider.id)
    if conn is not None:
        _install_handlers(provider.id, conn.app)
    return adapter


register_adapter_factory(ChannelProviderType.TELEGRAM, _telegram_factory)


__all__ = ["_telegram_factory"]
