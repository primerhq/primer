"""DiscordChannelAdapter — one per Channel row."""

from __future__ import annotations

import logging
from typing import Any

from primer.channel.adapter import (
    ChannelAdapter, PromptEnvelope, ResponseEnvelope,
)
from primer.channel.discord.connection import DISCORD_CONNECTIONS
from primer.channel.discord.views import ApprovalView
from primer.model.channel import Channel, ChannelProvider
from primer.model.except_ import ProviderError


logger = logging.getLogger(__name__)


class DiscordChannelAdapter(ChannelAdapter):
    """Per-channel Discord adapter."""

    def __init__(
        self, *, provider: ChannelProvider, channel: Channel, inbox,
    ) -> None:
        self._provider = provider
        self._channel = channel
        self._inbox = inbox
        self._client: Any | None = None
        # thread_id_str → {workspace_id, session_id, tool_call_id}
        self._thread_to_ids: dict[str, dict[str, str]] = {}

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
        if envelope.kind == "tool_approval":
            view = ApprovalView(
                ws=envelope.workspace_id,
                sid=envelope.session_id,
                tcid=envelope.tool_call_id,
            )
            msg = await channel.send(content=envelope.prompt, view=view)
            return {"message_id": getattr(msg, "id", 0)}
        elif envelope.kind == "ask_user":
            msg = await channel.send(content=envelope.prompt)
            thread = await msg.create_thread(
                name=(envelope.prompt or "agent question")[:80],
                auto_archive_duration=60,
            )
            tid = str(thread.id)
            self._thread_to_ids[tid] = {
                "workspace_id": envelope.workspace_id,
                "session_id": envelope.session_id,
                "tool_call_id": envelope.tool_call_id,
            }
            try:
                await thread.send("Reply in this thread to answer.")
            except Exception:
                logger.exception("discord: thread send failed")
            return {"message_id": getattr(msg, "id", 0), "thread_id": tid}
        else:
            raise ProviderError(f"unknown envelope kind {envelope.kind!r}")

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


__all__ = ["DiscordChannelAdapter"]
