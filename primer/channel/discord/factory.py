"""Register the Discord adapter factory + install gateway handlers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands

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


async def _route_channel_event(
    adapter: Any, provider_id: str, message: "discord.Message",
) -> bool:
    """Normalize a fresh inbound Discord message and, when a channel-trigger
    rule matches it, fire that rule.

    Returns ``True`` iff a rule matched and was dispatched - in which case the
    caller MUST skip the legacy chat-surface dispatch, so the message is not
    delivered twice (once as a rule action, once as a default chat message).
    Returns ``False`` for correlated replies and unmatched messages, leaving
    the caller's chat dispatch to own delivery.

    Builds the small dict envelope ``{"type": "message", "payload": {...}}`` the
    provider :class:`DiscordEventNormalizer` consumes (so the normalizer stays
    SDK-free) - deriving the channel ``kind`` discriminator from the runtime
    ``discord`` channel type. Best effort: any failure is logged and swallowed
    (returning ``False``) so it never breaks the chat-surface dispatch."""
    router = adapter._inbound_router()
    if router is None:
        return False
    try:
        from primer.channel.discord.normalizer import DiscordEventNormalizer

        ch = message.channel
        if isinstance(ch, discord.Thread):
            ch_kind = "thread"
            parent_id = getattr(ch, "parent_id", None)
        elif isinstance(ch, discord.DMChannel):
            ch_kind = "dm"
            parent_id = None
        else:
            ch_kind = "text"
            parent_id = None
        author = message.author
        payload = {
            "id": message.id,
            "content": message.content or "",
            "author": {
                "id": getattr(author, "id", None) if author else None,
                "name": getattr(author, "name", None) if author else None,
                "display_name": getattr(author, "display_name", None)
                if author
                else None,
                "bot": bool(getattr(author, "bot", False)) if author else False,
            },
            "channel": {
                "id": getattr(ch, "id", None),
                "kind": ch_kind,
                "parent_id": parent_id,
            },
        }
        normalizer = DiscordEventNormalizer(provider_id=provider_id)
        event = await normalizer.normalize({"type": "message", "payload": payload})
        if event is None:
            return False
        media_parts = await adapter.collect_inbound_media(message)
        await router.route_event(
            event=event, channel=adapter._channel,
            media_parts=media_parts or None,
        )
        return True
    except Exception:  # noqa: BLE001 -- never break chat-surface dispatch
        logger.exception("discord: channel-event routing failed")
        return False


def _install_handlers(provider_id: str, client: Any, channel: Channel) -> None:
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
                user_id=interaction.user.id if interaction.user else None,
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
                    user_id=submitted.user.id if submitted.user else None,
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
                user_id=interaction.user.id if interaction.user else None,
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
            # Session-prompt reply: the store is the authoritative source.
            # A thread reply that matches a parked ask_user correlation is
            # consumed here before chat-surface dispatch.
            sp = getattr(adapter, "_sp", None)
            if sp is not None:
                from primer.channel.correlation import CorrelationStore
                try:
                    rec = await CorrelationStore(sp).lookup(
                        adapter._channel.id, str(thread_id),
                    )
                except Exception:
                    rec = None
                if rec is not None and rec.kind == "session":
                    await adapter._handle_text_reply(
                        workspace_id=rec.workspace_id,
                        session_id=rec.session_id,
                        tool_call_id=rec.tool_call_id,
                        text=message.content or "",
                        user_id=message.author.id if message.author else None,
                    )
                    try:
                        await CorrelationStore(sp).clear(
                            adapter._channel.id, str(thread_id),
                        )
                    except Exception:
                        pass
                    return
            # Chat-surface dispatch: an in-thread message routes to that
            # thread's chat (thread id = the discord thread id).
            if getattr(adapter, "_sp", None) is None:
                return
            # Every inbound message is a routed event (S6 section 5).
            await _route_channel_event(adapter, provider_id, message)
            return
        # Top-level message in the channel: routed as an event, which fires
        # the channel triggers that create and map its session.
        channel_id = str(getattr(message.channel, "id", "") or "")
        adapter = entry.adapters_by_channel_id.get(channel_id)
        if adapter is None or getattr(adapter, "_sp", None) is None:
            return
        await _route_channel_event(adapter, provider_id, message)

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
    claim_engine=None,
    artifact_registry=None,
    workspace_registry=None,
    scheduler=None,
    **_kw,
):
    adapter = DiscordChannelAdapter(
        provider=provider, channel=channel, inbox=inbox,
        storage_provider=storage_provider, event_bus=event_bus,
        claim_engine=claim_engine, artifact_registry=artifact_registry,
        workspace_registry=workspace_registry, scheduler=scheduler,
    )
    await adapter.initialize()
    conn = DISCORD_CONNECTIONS.entry(provider.id)
    if conn is not None:
        _install_handlers(provider.id, conn.client, channel)
    return adapter


register_adapter_factory(ChannelProviderType.DISCORD, _discord_factory)


__all__ = ["_discord_factory"]
