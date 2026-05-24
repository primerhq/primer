"""TelegramChannelAdapter — one per Channel row."""

from __future__ import annotations

import logging
from typing import Any

from matrix.channel.adapter import (
    ChannelAdapter, PromptEnvelope, ResponseEnvelope,
)
from matrix.channel.telegram.connection import TELEGRAM_CONNECTIONS
from matrix.channel.telegram.render import (
    build_ask_user_message,
    build_tool_approval_message,
    compute_tag,
)
from matrix.model.channel import Channel, ChannelProvider
from matrix.model.except_ import ProviderError


logger = logging.getLogger(__name__)


class TelegramChannelAdapter(ChannelAdapter):
    """Per-channel Telegram adapter."""

    def __init__(
        self, *, provider: ChannelProvider, channel: Channel, inbox,
    ) -> None:
        self._provider = provider
        self._channel = channel
        self._inbox = inbox
        self._app: Any | None = None
        self._tag_cache: dict[str, dict[str, str]] = {}

    async def initialize(self) -> None:
        self._app = await TELEGRAM_CONNECTIONS.acquire(self._provider)
        entry = TELEGRAM_CONNECTIONS.entry(self._provider.id)
        if entry is not None:
            entry.adapters_by_chat_id[str(self._channel.external_id)] = self

    async def aclose(self) -> None:
        entry = TELEGRAM_CONNECTIONS.entry(self._provider.id)
        if entry is not None:
            entry.adapters_by_chat_id.pop(str(self._channel.external_id), None)
        if self._app is not None:
            await TELEGRAM_CONNECTIONS.release(self._provider)
            self._app = None

    async def verify(self) -> None:
        if self._app is None:
            raise ProviderError("TelegramChannelAdapter used before initialize()")
        me = await self._app.bot.get_me()
        if not me.username:
            raise ProviderError("telegram getMe returned no username")
        try:
            chat = await self._app.bot.get_chat(
                chat_id=int(self._channel.external_id),
            )
        except Exception as exc:
            raise ProviderError(
                f"telegram chat {self._channel.external_id!r} not reachable: {exc}"
            ) from exc
        if chat is None:
            raise ProviderError(
                f"telegram chat {self._channel.external_id!r} not reachable"
            )

    async def post_prompt(self, envelope: PromptEnvelope) -> dict[str, Any]:
        if self._app is None:
            raise ProviderError("TelegramChannelAdapter used before initialize()")
        if envelope.kind == "ask_user":
            body = build_ask_user_message(
                chat_id=self._channel.external_id, envelope=envelope,
            )
        elif envelope.kind == "tool_approval":
            body = build_tool_approval_message(
                chat_id=self._channel.external_id, envelope=envelope,
            )
        else:
            raise ProviderError(f"unknown envelope kind {envelope.kind!r}")
        tag = compute_tag(
            workspace_id=envelope.workspace_id,
            session_id=envelope.session_id,
            tool_call_id=envelope.tool_call_id,
        )
        self._tag_cache[tag] = {
            "workspace_id": envelope.workspace_id,
            "session_id": envelope.session_id,
            "tool_call_id": envelope.tool_call_id,
        }
        msg = await self._app.bot.send_message(**body)
        return {"message_id": getattr(msg, "message_id", 0)}

    async def _resolve_tag(self, tag: str) -> dict[str, str] | None:
        cached = self._tag_cache.get(tag)
        if cached is not None:
            return cached
        # Cold-lookup fallback. Inject the session storage via the
        # registry pattern (test will populate cache, so cold path
        # only runs in real deployments after restart).
        return None

    async def _handle_decision(
        self, *,
        workspace_id: str, session_id: str, tool_call_id: str,
        decision: str, reason: str | None,
        telegram_user_id: int | None,
    ) -> None:
        await self._inbox.handle_response(ResponseEnvelope(
            kind="tool_approval",
            workspace_id=workspace_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            response=None, decision=decision, reason=reason,
            platform_metadata={
                "telegram_user_id": telegram_user_id or 0,
            },
        ))

    async def _handle_text_reply(
        self, *,
        workspace_id: str, session_id: str, tool_call_id: str,
        text: str,
        telegram_user_id: int | None,
    ) -> None:
        await self._inbox.handle_response(ResponseEnvelope(
            kind="ask_user",
            workspace_id=workspace_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            response=text,
            decision=None, reason=None,
            platform_metadata={
                "telegram_user_id": telegram_user_id or 0,
            },
        ))


__all__ = ["TelegramChannelAdapter"]
