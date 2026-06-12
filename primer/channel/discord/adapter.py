"""DiscordChannelAdapter — one per Channel row."""

from __future__ import annotations

import logging
from typing import Any

from primer.channel.adapter import (
    ChannelAdapter, PromptEnvelope, ResponseEnvelope,
    format_tool_args, session_thread_label,
)
from primer.channel.discord.connection import DISCORD_CONNECTIONS
from primer.channel.discord.views import ApprovalView
from primer.model.channel import Channel, ChannelProvider
from primer.model.except_ import ProviderError


logger = logging.getLogger(__name__)

# Discord message content caps at 2000 chars; keep args well under it.
_DISCORD_ARGS_MAX = 1700


def format_approval_content(envelope: PromptEnvelope) -> str:
    """Render a tool-approval prompt as Discord markdown: tool name plus a
    pretty-printed JSON code block, instead of the raw ``prompt`` string.
    """
    tool_name = envelope.tool_name or "(unknown tool)"
    args_json = format_tool_args(envelope.tool_args)
    if len(args_json) > _DISCORD_ARGS_MAX:
        args_json = args_json[:_DISCORD_ARGS_MAX] + "\n... (truncated)"
    return (
        ":lock: **Tool approval requested**\n"
        f"**Tool:** `{tool_name}`\n"
        f"**Arguments:**\n```json\n{args_json}\n```"
    )


class DiscordChannelAdapter(ChannelAdapter):
    """Per-channel Discord adapter."""

    def __init__(
        self, *, provider: ChannelProvider, channel: Channel, inbox,
        storage_provider=None, event_bus=None, claim_engine=None,
    ) -> None:
        self._provider = provider
        self._channel = channel
        self._inbox = inbox
        # Chat-surface wiring. Optional so existing callers (session/workspace
        # channels) keep working; the chat dispatch path stays inactive when
        # _sp is None.
        self._sp = storage_provider
        self._bus = event_bus
        self._claim_engine = claim_engine
        self._client: Any | None = None
        # session_id → discord Thread id (one conversation thread per session)
        self._session_threads: dict[str, int] = {}
        # thread_id (int) → {workspace_id, session_id, tool_call_id} for the
        # ask_user currently awaiting a reply in that thread (sessions park on
        # one prompt at a time).
        self._pending_ask: dict[int, dict[str, str]] = {}

    async def initialize(self) -> None:
        self._client = await DISCORD_CONNECTIONS.acquire(self._provider)
        entry = DISCORD_CONNECTIONS.entry(self._provider.id)
        if entry is not None:
            entry.adapters_by_channel_id[str(self._channel.external_id)] = self

    async def aclose(self) -> None:
        entry = DISCORD_CONNECTIONS.entry(self._provider.id)
        if entry is not None:
            entry.adapters_by_channel_id.pop(str(self._channel.external_id), None)
        if self._client is not None:
            await DISCORD_CONNECTIONS.release(self._provider)
            self._client = None

    async def verify(self) -> None:
        if self._client is None:
            raise ProviderError("DiscordChannelAdapter used before initialize()")
        me = self._client.user
        if me is None or getattr(me, "id", 0) == 0:
            raise ProviderError("discord gateway login failed (no bot user)")
        try:
            channel = self._client.get_channel(int(self._channel.external_id))
            if channel is None:
                channel = await self._client.fetch_channel(
                    int(self._channel.external_id),
                )
        except Exception as exc:
            raise ProviderError(
                f"discord channel {self._channel.external_id!r} not reachable: {exc}"
            ) from exc
        if channel is None:
            raise ProviderError(
                f"discord channel {self._channel.external_id!r} not reachable"
            )

    async def post_prompt(self, envelope: PromptEnvelope) -> dict[str, Any]:
        if self._client is None:
            raise ProviderError("DiscordChannelAdapter used before initialize()")
        channel = self._client.get_channel(int(self._channel.external_id))
        if channel is None:
            channel = await self._client.fetch_channel(
                int(self._channel.external_id),
            )
        if channel is None:
            raise ProviderError(
                f"discord channel {self._channel.external_id!r} not reachable"
            )
        thread = await self._session_thread(channel, envelope.session_id)
        if envelope.kind == "tool_approval":
            view = ApprovalView(
                ws=envelope.workspace_id,
                sid=envelope.session_id,
                tcid=envelope.tool_call_id,
            )
            msg = await thread.send(
                content=format_approval_content(envelope), view=view,
            )
            return {"message_id": getattr(msg, "id", 0), "thread_id": thread.id}
        elif envelope.kind == "ask_user":
            msg = await thread.send(content=envelope.prompt)
            self._pending_ask[thread.id] = {
                "workspace_id": envelope.workspace_id,
                "session_id": envelope.session_id,
                "tool_call_id": envelope.tool_call_id,
            }
            return {"message_id": getattr(msg, "id", 0), "thread_id": thread.id}
        elif envelope.kind == "inform":
            msg = await thread.send(content=envelope.prompt)
            return {"message_id": getattr(msg, "id", 0), "thread_id": thread.id}
        else:
            raise ProviderError(f"unknown envelope kind {envelope.kind!r}")

    async def _session_thread(self, channel: Any, session_id: str) -> Any:
        """Get-or-create the one conversation thread for this session.

        The first prompt posts a small anchor message and opens a named thread
        off it; every later prompt for the same session (ask or approval) is
        sent into that thread.
        """
        tid = self._session_threads.get(session_id)
        if tid is not None:
            thread = self._client.get_channel(tid)
            if thread is None:
                try:
                    thread = await self._client.fetch_channel(tid)
                except Exception:
                    thread = None
            if thread is not None:
                return thread
        label = session_thread_label(session_id)
        anchor = await channel.send(content=f":thread: {label}")
        thread = await anchor.create_thread(
            name=label[:100], auto_archive_duration=60,
        )
        self._session_threads[session_id] = thread.id
        return thread

    async def _handle_decision(
        self, *,
        workspace_id: str, session_id: str, tool_call_id: str,
        decision: str, reason: str | None,
        discord_user_id: int | None,
    ) -> None:
        await self._inbox.handle_response(ResponseEnvelope(
            kind="tool_approval",
            workspace_id=workspace_id, session_id=session_id,
            tool_call_id=tool_call_id,
            response=None, decision=decision, reason=reason,
            platform_metadata={"discord_user_id": discord_user_id or 0},
        ))

    async def _handle_text_reply(
        self, *,
        workspace_id: str, session_id: str, tool_call_id: str,
        text: str,
        discord_user_id: int | None,
    ) -> None:
        await self._inbox.handle_response(ResponseEnvelope(
            kind="ask_user",
            workspace_id=workspace_id, session_id=session_id,
            tool_call_id=tool_call_id,
            response=text, decision=None, reason=None,
            platform_metadata={"discord_user_id": discord_user_id or 0},
        ))

    async def _resolve_chat_thread(self, thread_ts: str | None) -> Any:
        """Resolve a discord.py thread/channel object from a thread anchor id.

        ``thread_ts`` is the string Discord thread (or message) id stored in
        ``ChatChannelBinding.thread_external_id``.  We try the client cache
        first, then fall back to a REST fetch, mirroring the lookup path in
        ``_session_thread``.  If ``thread_ts`` is None we fall back to the
        parent channel itself.
        """
        if self._client is None:
            raise ProviderError("DiscordChannelAdapter used before initialize()")
        if thread_ts is not None:
            tid = int(thread_ts)
            thread = self._client.get_channel(tid)
            if thread is None:
                try:
                    thread = await self._client.fetch_channel(tid)
                except Exception:
                    thread = None
            if thread is not None:
                return thread
        # Fall back to the parent channel
        parent_id = int(self._channel.external_id)
        channel = self._client.get_channel(parent_id)
        if channel is None:
            channel = await self._client.fetch_channel(parent_id)
        if channel is None:
            raise ProviderError(
                f"discord channel {self._channel.external_id!r} not reachable"
            )
        return channel

    async def post_chat_message(
        self, text: str, *, thread_ts: str | None = None
    ) -> dict[str, Any]:
        """Full-payload outbound relay into the chat's thread."""
        target = await self._resolve_chat_thread(thread_ts)
        await target.send(content=text)
        return {"thread_id": thread_ts}

    async def handle_inbound_chat_message(
        self, *, thread_id: str | None, message_id: str,
        sender_name: str, text: str,
    ):
        """Multi-type inbound: a top-level message opens a new thread-chat; an
        in-thread message routes to that thread's chat. The thread id is the
        message id on a top-level message (the new thread anchors on it)."""
        from primer.channel.chat_inbox import ChatResponseInbox
        from primer.channel.chat_router import ChatChannelRouter
        thread_external_id = thread_id or message_id
        gate_inbox = ChatResponseInbox(
            storage_provider=self._sp, event_bus=self._bus,
            claim_engine=self._claim_engine)
        router = ChatChannelRouter(
            storage_provider=self._sp, event_bus=self._bus, gate_inbox=gate_inbox,
            claim_engine=self._claim_engine)
        chat, _ = await router.deliver_message(
            channel_id=self._channel.id, thread_external_id=thread_external_id,
            supports_threads=True, sender_name=sender_name, text=text)
        return chat


__all__ = ["DiscordChannelAdapter"]
