"""Register the Discord adapter factory + install gateway handlers."""

from __future__ import annotations

import logging
from typing import Any

import discord

from primer.channel.discord.adapter import DiscordChannelAdapter
from primer.channel.discord.connection import DISCORD_CONNECTIONS
from primer.channel.discord.views import (
    REJECT_MODAL_CUSTOM_ID_PREFIX,
    build_reject_modal,
    decode_custom_id,
)
from primer.channel.factory import register_adapter_factory
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
)


logger = logging.getLogger(__name__)


_HANDLERS_INSTALLED: set[str] = set()


def _install_handlers(provider_id: str, client: Any) -> None:
    if provider_id in _HANDLERS_INSTALLED:
        return
    _HANDLERS_INSTALLED.add(provider_id)

    async def _on_interaction(interaction: discord.Interaction):
        data = interaction.data or {}
        custom_id = data.get("custom_id") if isinstance(data, dict) else None
        if not custom_id:
            return
        parsed = decode_custom_id(custom_id)
        if parsed is None:
            return
        verb, ws, sid, tcid = parsed
        # Approval buttons now live inside the session thread, so the
        # interaction's channel is the Thread; resolve the adapter via the
        # thread's parent channel (where the adapter is registered).
        ch = interaction.channel
        parent = getattr(ch, "parent_id", None)
        channel_id = str(parent) if parent else str(interaction.channel_id or "")
        entry = DISCORD_CONNECTIONS.entry(provider_id)
        if entry is None:
            return
        adapter = entry.adapters_by_channel_id.get(channel_id)

        if verb == "approve":
            if adapter is None:
                return
            # Ack + strip the buttons first (Discord drops any interaction not
            # answered within ~3s), then record the decision.
            try:
                await interaction.response.edit_message(
                    content=(
                        (interaction.message.content or "")
                        + "\n\n✓ Approved by <@" + str(interaction.user.id) + ">"
                    ),
                    view=None,
                )
            except Exception:
                logger.exception("discord: edit_message failed")
            await adapter._handle_decision(
                workspace_id=ws, session_id=sid, tool_call_id=tcid,
                decision="approved", reason=None,
                discord_user_id=interaction.user.id if interaction.user else None,
            )
            return

        if verb == "reject":
            if adapter is None:
                return
            # Capture the original message now; modal-submit interactions
            # don't carry it, and we want to strip the buttons afterwards.
            original_message = interaction.message

            async def _on_modal_submit(submitted: discord.Interaction, reason_text: str):
                # Ack the modal submission first (3s window), then record the
                # decision and strip the buttons on the original message.
                try:
                    await submitted.response.send_message(
                        content="✗ Rejection recorded.", ephemeral=True,
                    )
                except Exception:
                    logger.exception("discord: modal ack failed")
                await adapter._handle_decision(
                    workspace_id=ws, session_id=sid, tool_call_id=tcid,
                    decision="rejected", reason=reason_text or None,
                    discord_user_id=submitted.user.id if submitted.user else None,
                )
                try:
                    if original_message is not None:
                        note = (
                            "\n\n✗ Rejected by <@"
                            + str(submitted.user.id if submitted.user else "")
                            + ">"
                        )
                        if reason_text:
                            note += ": " + reason_text
                        await original_message.edit(
                            content=(original_message.content or "") + note,
                            view=None,
                        )
                except Exception:
                    logger.exception("discord: reject edit_message failed")

            modal = build_reject_modal(
                ws=ws, sid=sid, tcid=tcid, on_submit=_on_modal_submit,
            )
            await interaction.response.send_modal(modal)
            return

        # Modal-submit interactions arrive here too, but we use
        # the modal's on_submit closure above to route them — so
        # this branch only fires if something registers a modal
        # WITHOUT a closure (shouldn't happen in this codebase).
        if verb == REJECT_MODAL_CUSTOM_ID_PREFIX and adapter is not None:
            # Defensive fallback — pull reason from components.
            comps = interaction.data.get("components") or []
            reason = ""
            for row in comps:
                for c in (row or {}).get("components", []):
                    if c.get("custom_id") == "reason" or c.get("type") == 4:
                        reason = c.get("value") or reason
            await adapter._handle_decision(
                workspace_id=ws, session_id=sid, tool_call_id=tcid,
                decision="rejected", reason=reason or None,
                discord_user_id=interaction.user.id if interaction.user else None,
            )

    async def _on_message(message: discord.Message):
        if message.author and message.author.bot:
            return
        entry = DISCORD_CONNECTIONS.entry(provider_id)
        if entry is None:
            return
        in_thread = isinstance(message.channel, discord.Thread)
        if in_thread:
            thread_id = message.channel.id
            parent_id = str(message.channel.parent_id or "")
            adapter = entry.adapters_by_channel_id.get(parent_id)
            if adapter is None:
                return
            # Session-prompt reply: an ask_user parked on this thread takes
            # precedence so existing session gates keep working.
            ids = adapter._pending_ask.get(thread_id)
            if ids is not None:
                await adapter._handle_text_reply(
                    **ids, text=message.content or "",
                    discord_user_id=message.author.id if message.author else None,
                )
                # Consume: the ask is answered, so the next thread reply won't
                # re-fire until another ask_user parks in this session.
                adapter._pending_ask.pop(thread_id, None)
                return
            # Chat-surface dispatch: an in-thread message routes to that
            # thread's chat (thread id = the discord thread id).
            if getattr(adapter, "_sp", None) is None:
                return
            sender_name = (
                getattr(message.author, "display_name", None)
                or getattr(message.author, "name", None) or "user"
            )
            await adapter.handle_inbound_chat_message(
                thread_id=str(thread_id), message_id=str(message.id),
                sender_name=sender_name, text=message.content or "",
            )
            return
        # Top-level message in the channel: open a new thread-chat anchored on
        # the message id. Only on chat-enabled adapters.
        channel_id = str(getattr(message.channel, "id", "") or "")
        adapter = entry.adapters_by_channel_id.get(channel_id)
        if adapter is None or getattr(adapter, "_sp", None) is None:
            return
        sender_name = (
            getattr(message.author, "display_name", None)
            or getattr(message.author, "name", None) or "user"
        )
        await adapter.handle_inbound_chat_message(
            thread_id=None, message_id=str(message.id),
            sender_name=sender_name, text=message.content or "",
        )

    # Bind the handlers to the real gateway event names. The base
    # ``discord.Client`` dispatches by looking up ``self.on_<event>`` (this is
    # exactly what ``@client.event`` does via setattr on the coroutine's
    # __name__). Our handlers are named ``_on_interaction``/``_on_message``, so
    # ``client.event`` would store them under the wrong attribute and they'd
    # never fire; ``add_listener`` doesn't exist on the base Client. Assigning
    # the correctly-named attributes directly is the supported registration.
    client.on_interaction = _on_interaction
    client.on_message = _on_message


async def _discord_factory(
    provider: ChannelProvider,
    channel: Channel,
    inbox,
    *,
    storage_provider=None,
    event_bus=None,
    **_kw,
):
    adapter = DiscordChannelAdapter(
        provider=provider, channel=channel, inbox=inbox,
        storage_provider=storage_provider, event_bus=event_bus,
    )
    await adapter.initialize()
    conn = DISCORD_CONNECTIONS.entry(provider.id)
    if conn is not None:
        _install_handlers(provider.id, conn.client)
    return adapter


register_adapter_factory(ChannelProviderType.DISCORD, _discord_factory)


__all__ = ["_discord_factory"]
