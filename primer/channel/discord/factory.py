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
        channel_id = str(interaction.channel_id or "")
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
        if not isinstance(message.channel, discord.Thread):
            return
        thread_id = str(message.channel.id)
        parent_id = str(message.channel.parent_id or "")
        entry = DISCORD_CONNECTIONS.entry(provider_id)
        if entry is None:
            return
        adapter = entry.adapters_by_channel_id.get(parent_id)
        if adapter is None:
            return
        ids = adapter._thread_to_ids.get(thread_id)
        if ids is None:
            return
        await adapter._handle_text_reply(
            **ids, text=message.content or "",
            discord_user_id=message.author.id if message.author else None,
        )
        # Single-use: drop the entry so subsequent thread messages don't fire.
        adapter._thread_to_ids.pop(thread_id, None)

    # NB: register with explicit event names. ``client.event`` keys off the
    # coroutine's __name__, which here is ``_on_interaction``/``_on_message``
    # (leading underscore), so the handlers would be stored under the wrong
    # attribute and never dispatched. ``add_listener(func, name)`` binds them
    # to the real ``on_interaction``/``on_message`` gateway events.
    client.add_listener(_on_interaction, "on_interaction")
    client.add_listener(_on_message, "on_message")


async def _discord_factory(
    provider: ChannelProvider,
    channel: Channel,
    inbox,
):
    adapter = DiscordChannelAdapter(
        provider=provider, channel=channel, inbox=inbox,
    )
    await adapter.initialize()
    conn = DISCORD_CONNECTIONS.entry(provider.id)
    if conn is not None:
        _install_handlers(provider.id, conn.client)
    return adapter


register_adapter_factory(ChannelProviderType.DISCORD, _discord_factory)


__all__ = ["_discord_factory"]
