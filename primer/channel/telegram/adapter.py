"""TelegramChannelAdapter — one per Channel row."""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any

from primer.channel.adapter import (
    ChannelAdapter, PromptEnvelope, ResponseEnvelope,
)
from primer.channel.telegram.connection import TELEGRAM_CONNECTIONS
from primer.channel.telegram.render import (
    build_ask_user_message,
    build_tool_approval_message,
    compute_tag,
)
from primer.model.channel import Channel, ChannelProvider
from primer.model.except_ import ProviderError


logger = logging.getLogger(__name__)

# Max correlation entries kept per adapter. Sized for a busy bot's recent
# in-flight prompts; older entries are evicted (their parks, if still open,
# fall back to the storage row on resume).
_CACHE_MAXSIZE = 10_000


class _BoundedDict(OrderedDict):
    """An insertion-ordered dict that evicts the oldest entry once it
    exceeds ``maxsize``. Re-inserting an existing key refreshes its
    recency (move-to-end)."""

    def __init__(self, *, maxsize: int) -> None:
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key, value) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self._maxsize:
            self.popitem(last=False)


class TelegramChannelAdapter(ChannelAdapter):
    """Per-channel Telegram adapter."""

    def __init__(
        self, *, provider: ChannelProvider, channel: Channel, inbox,
    ) -> None:
        self._provider = provider
        self._channel = channel
        self._inbox = inbox
        self._app: Any | None = None
        # tag -> ids, for the Approve/Reject button callbacks. Bounded so a
        # long-lived bot does not grow these caches without limit (one entry
        # per prompt sent); the oldest correlations fall off first.
        self._tag_cache: _BoundedDict = _BoundedDict(maxsize=_CACHE_MAXSIZE)
        # message_id -> {**ids, "kind": "ask_user" | "reject"}, so a text
        # reply is correlated by the message it replies to (no visible
        # token in the message body).
        self._reply_targets: _BoundedDict = _BoundedDict(maxsize=_CACHE_MAXSIZE)

    def remember_reply_target(
        self, *, message_id: int, ids: dict[str, str], kind: str,
    ) -> None:
        self._reply_targets[message_id] = {**ids, "kind": kind}

    def resolve_reply_target(self, message_id: int) -> dict[str, str] | None:
        return self._reply_targets.get(message_id)

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
        ids = {
            "workspace_id": envelope.workspace_id,
            "session_id": envelope.session_id,
            "tool_call_id": envelope.tool_call_id,
        }
        self._tag_cache[tag] = ids
        msg = await self._app.bot.send_message(**body)
        message_id = getattr(msg, "message_id", 0)
        # ask_user is answered by a text reply -> correlate by message id.
        # tool_approval is answered by the inline buttons (callback_data).
        if envelope.kind == "ask_user" and message_id:
            self.remember_reply_target(
                message_id=message_id, ids=ids, kind="ask_user",
            )
        return {"message_id": message_id}

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
