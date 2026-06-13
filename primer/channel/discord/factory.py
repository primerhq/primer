"""Register the Discord adapter factory + install gateway handlers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands

from primer.channel.discord.adapter import DiscordChannelAdapter
from primer.channel.discord.commands import (
    agent_autocomplete_choices,
    handle_app_command,
)
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
                        discord_user_id=message.author.id if message.author else None,
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
            sender_name = (
                getattr(message.author, "display_name", None)
                or getattr(message.author, "name", None) or "user"
            )
            await adapter.handle_inbound_chat_message(
                thread_id=str(thread_id), message_id=str(message.id),
                sender_name=sender_name, text=message.content or "",
                attachments=list(getattr(message, "attachments", None) or []),
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
            attachments=list(getattr(message, "attachments", None) or []),
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

    # ------------------------------------------------------------------ #
    # Application-command tree (slash commands + autocomplete)
    # ------------------------------------------------------------------ #
    # Build a CommandTree against the client.  We register two commands:
    #   /agent [value]  - with-value: switch thread's chat agent; no-arg: picker
    #   /help           - show available commands
    # (No /new or /list: a new thread is a new chat; threads are the chat list.)
    #
    # NOTE: syncing the tree to Discord requires `await tree.sync()` after the
    # client reaches READY. We wire a one-shot on_ready handler that syncs
    # globally (all guilds). This is harmless when the tree is already in sync
    # and is rate-limited by Discord to ~once per hour in production. Sync is
    # NOT required for the unit tests (pure helpers, no live gateway).
    # ------------------------------------------------------------------ #
    try:
        tree = app_commands.CommandTree(client)
    except Exception:
        logger.exception("discord: app_commands.CommandTree creation failed")
        return

    def _resolve_sp(entry_id: str):
        """Return the storage_provider from the first registered adapter, if any."""
        entry = DISCORD_CONNECTIONS.entry(entry_id)
        if entry is None:
            return None
        for adapter in entry.adapters_by_channel_id.values():
            sp = getattr(adapter, "_sp", None)
            if sp is not None:
                return sp
        return None

    def _resolve_adapter(interaction: discord.Interaction):
        """Resolve the chat adapter for the interaction's channel/thread.

        Commands run in a thread report the thread as ``interaction.channel``;
        the adapter is keyed by the PARENT channel snowflake. Returns the
        adapter (which carries ``_sp`` and the primer ``_channel``) or None.
        """
        entry = DISCORD_CONNECTIONS.entry(provider_id)
        if entry is None:
            return None
        ch = interaction.channel
        parent = (
            ch.parent_id if isinstance(ch, discord.Thread)
            else interaction.channel_id
        )
        if parent is None:
            return None
        return entry.adapters_by_channel_id.get(str(parent))

    # No /new or /list on Discord: a new thread is a new chat, and the
    # channel's threads are the chat list.

    @tree.command(name="agent", description="Switch agent for this thread (or pick one)")
    @app_commands.describe(value="Agent ID to switch to; omit to list available agents")
    async def _cmd_agent(interaction: discord.Interaction, value: str = ""):
        adapter = _resolve_adapter(interaction)
        sp = getattr(adapter, "_sp", None) if adapter is not None else None
        if sp is None:
            await interaction.response.send_message(
                "Chat not configured for this channel.", ephemeral=True)
            return
        channel_id = adapter._channel.id
        ch = interaction.channel
        thread_id = (
            str(ch.id) if isinstance(ch, discord.Thread) else None
        )
        # Agent switching is per-thread/per-chat: require a thread.
        if thread_id is None:
            await interaction.response.send_message(
                "Run /agent inside a chat thread to switch its agent.",
                ephemeral=True)
            return
        try:
            res = await handle_app_command(
                storage_provider=sp, command="agent", channel_id=channel_id,
                arg=value or None, thread_id=thread_id)
        except Exception as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        # Any notice (a direct /agent value:<id> switch, a disabled-switch
        # short-circuit, or a not-allowed agent) carries its message in
        # res.text; surface it verbatim. Only the no-arg, switching-enabled
        # path returns an agent_picker to render as a dropdown.
        if res.kind == "notice":
            await interaction.response.send_message(
                res.text or "Done.", ephemeral=True)
            return

        from primer.channel.discord.views import AgentSelectView
        opts = res.items or []
        if not opts:
            await interaction.response.send_message("No agents.", ephemeral=True)
            return

        async def _on_pick(pick_inter: discord.Interaction, agent_id: str) -> None:
            try:
                r2 = await handle_app_command(
                    storage_provider=sp, command="agent",
                    channel_id=channel_id, arg=agent_id, thread_id=thread_id)
                msg = r2.text or "Agent switched."
            except Exception as exc:
                msg = f"Could not switch: {exc}"
            # Confirm the switch (ephemeral). edit the picker message so the
            # dropdown is replaced by the confirmation.
            try:
                await pick_inter.response.edit_message(content=f"OK - {msg}", view=None)
            except Exception:
                await pick_inter.response.send_message(msg, ephemeral=True)

        view = AgentSelectView(options=opts, on_pick=_on_pick)
        await interaction.response.send_message(
            "Pick an agent:", view=view, ephemeral=True)
        return

    @tree.command(name="help", description="Show available commands")
    async def _cmd_help(interaction: discord.Interaction):
        try:
            res = await handle_app_command(
                storage_provider=_resolve_sp(provider_id) or None,
                command="help", channel_id=str(interaction.channel_id or ""),
                arg=None, thread_id=None)
            text = res.text or "No help available."
        except Exception:
            from primer.channel.commands import help_text
            text = help_text(supports_threads=True)
        await interaction.response.send_message(text, ephemeral=True)

    @_cmd_agent.autocomplete("value")
    async def _agent_autocomplete(
        interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        sp = _resolve_sp(provider_id)
        if sp is None:
            return []
        try:
            choices = await agent_autocomplete_choices(
                storage_provider=sp, current=current)
        except Exception:
            logger.exception("discord: agent autocomplete failed")
            return []
        return [
            app_commands.Choice(name=c["name"], value=c["value"])
            for c in choices
        ]

    # Sync once the gateway is READY. on_ready can fire multiple times on
    # reconnects; guard with a flag so we only sync once per provider.
    _synced: list[bool] = [False]

    async def _sync_tree() -> None:
        if _synced[0]:
            return
        _synced[0] = True
        try:
            # Guild-scoped sync propagates instantly (global sync can take up
            # to ~1 hour). Resolve the guild from the configured channel.
            guild = None
            try:
                ch = client.get_channel(int(channel.external_id))
                if ch is None:
                    ch = await client.fetch_channel(int(channel.external_id))
                guild = getattr(ch, "guild", None)
            except Exception:
                guild = None
            if guild is not None:
                tree.copy_global_to(guild=guild)
                await tree.sync(guild=guild)
                logger.info(
                    "discord: app_commands synced to guild %s for provider %s",
                    guild.id, provider_id)
            else:
                await tree.sync()
                logger.info(
                    "discord: app_commands globally synced for provider %s",
                    provider_id)
        except Exception:
            _synced[0] = False
            logger.exception(
                "discord: tree.sync() failed for provider %s", provider_id)

    async def _on_ready() -> None:
        await _sync_tree()

    client.on_ready = _on_ready
    # The factory connects the client BEFORE installing handlers, so on_ready
    # may have already fired - sync now if the client is already ready.
    if client.is_ready():
        asyncio.create_task(_sync_tree())


async def _discord_factory(
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
    adapter = DiscordChannelAdapter(
        provider=provider, channel=channel, inbox=inbox,
        storage_provider=storage_provider, event_bus=event_bus,
        claim_engine=claim_engine, artifact_registry=artifact_registry,
    )
    await adapter.initialize()
    conn = DISCORD_CONNECTIONS.entry(provider.id)
    if conn is not None:
        _install_handlers(provider.id, conn.client, channel)
    return adapter


register_adapter_factory(ChannelProviderType.DISCORD, _discord_factory)


__all__ = ["_discord_factory"]
