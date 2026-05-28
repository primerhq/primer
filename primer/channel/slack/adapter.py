"""SlackChannelAdapter — one per Channel row.

Delegates connection lifecycle to the package-level
``SLACK_CONNECTIONS`` registry. Holds the per-channel rendering
+ inbound-routing logic. Multiple adapters for the same provider
share one Socket Mode connection.
"""

from __future__ import annotations

import logging
from typing import Any

from primer.channel.adapter import (
    ChannelAdapter, PromptEnvelope, ResponseEnvelope,
)
from primer.channel.slack.connection import SLACK_CONNECTIONS
from primer.channel.slack.render import (
    REJECT_MODAL_CALLBACK_ID,
    build_ask_user_message,
    build_reject_modal,
    build_tool_approval_message,
)
from primer.model.channel import Channel, ChannelProvider
from primer.model.except_ import ProviderError


logger = logging.getLogger(__name__)


def _get_web_client(conn: Any) -> Any:
    """Return the AsyncWebClient on the connection.

    slack_bolt exposes it as ``app.client``; we wrap that here so
    tests can override the lookup with a stub.
    """
    return conn.app.client


class SlackChannelAdapter(ChannelAdapter):
    """Per-channel Slack adapter."""

    def __init__(
        self,
        *,
        provider: ChannelProvider,
        channel: Channel,
        inbox,
    ) -> None:
        self._provider = provider
        self._channel = channel
        self._inbox = inbox
        self._conn: Any | None = None
        # Per-channel cache: maps (channel_id, thread_ts) → payload dict
        # populated at post-time; bounded by manual LRU eviction below.
        self._thread_payload_cache: dict[tuple[str, str], dict[str, Any]] = {}

    async def initialize(self) -> None:
        self._conn = await SLACK_CONNECTIONS.acquire(self._provider)
        # Register this adapter on the shared connection so inbound
        # handlers can dispatch by channel_id.
        entry = SLACK_CONNECTIONS.entry(self._provider.id)
        if entry is not None:
            entry.adapters_by_channel_id[self._channel.external_id] = self
        # Handlers are registered once-per-connection in Task 6 (factory).

    async def aclose(self) -> None:
        entry = SLACK_CONNECTIONS.entry(self._provider.id)
        if entry is not None:
            entry.adapters_by_channel_id.pop(self._channel.external_id, None)
        if self._conn is not None:
            await SLACK_CONNECTIONS.release(self._provider)
            self._conn = None

    async def verify(self) -> None:
        if self._conn is None:
            raise ProviderError("SlackChannelAdapter used before initialize()")
        client = _get_web_client(self._conn)
        auth = await client.auth_test()
        if not auth.get("ok"):
            raise ProviderError(
                f"slack auth.test failed: {auth.get('error', 'unknown')}"
            )
        info = await client.conversations_info(channel=self._channel.external_id)
        if not info.get("ok"):
            raise ProviderError(
                f"channel {self._channel.external_id!r} not reachable: "
                f"{info.get('error', 'unknown')}"
            )

    async def post_prompt(self, envelope: PromptEnvelope) -> dict[str, Any]:
        if self._conn is None:
            raise ProviderError("SlackChannelAdapter used before initialize()")
        client = _get_web_client(self._conn)
        if envelope.kind == "ask_user":
            body = build_ask_user_message(
                channel_id=self._channel.external_id, envelope=envelope,
            )
        elif envelope.kind == "tool_approval":
            body = build_tool_approval_message(
                channel_id=self._channel.external_id, envelope=envelope,
            )
        else:
            raise ProviderError(f"unknown envelope kind {envelope.kind!r}")
        resp = await client.chat_postMessage(**body)
        ts = resp.get("ts", "")
        if ts:
            self._thread_payload_cache[
                (self._channel.external_id, ts)
            ] = {
                "ws": envelope.workspace_id,
                "sid": envelope.session_id,
                "tcid": envelope.tool_call_id,
                "kind": envelope.kind,
            }
            self._lru_evict()
        return {"ts": ts, "channel": resp.get("channel", "")}

    def _lru_evict(self, cap: int = 1024) -> None:
        if len(self._thread_payload_cache) <= cap:
            return
        # Drop the oldest insertions until under cap.
        while len(self._thread_payload_cache) > cap:
            self._thread_payload_cache.pop(
                next(iter(self._thread_payload_cache)),
            )

    # ----- inbound helpers (called from handlers in factory.py) -----------

    async def _handle_decision(
        self,
        *,
        ws: str, sid: str, tcid: str,
        decision: str, reason: str | None,
        slack_user_id: str | None,
    ) -> None:
        await self._inbox.handle_response(ResponseEnvelope(
            kind="tool_approval",
            workspace_id=ws, session_id=sid, tool_call_id=tcid,
            response=None, decision=decision, reason=reason,
            platform_metadata={"slack_user_id": slack_user_id or ""},
        ))

    async def _handle_text_reply(
        self,
        *,
        ws: str, sid: str, tcid: str,
        text: str,
        slack_user_id: str | None,
    ) -> None:
        await self._inbox.handle_response(ResponseEnvelope(
            kind="ask_user",
            workspace_id=ws, session_id=sid, tool_call_id=tcid,
            response=text,
            decision=None, reason=None,
            platform_metadata={"slack_user_id": slack_user_id or ""},
        ))

    async def _lookup_thread_payload(
        self, *, channel_id: str, thread_ts: str,
    ) -> dict[str, Any] | None:
        cached = self._thread_payload_cache.get((channel_id, thread_ts))
        if cached is not None:
            return cached
        # Fall back to conversations.history with include_all_metadata.
        if self._conn is None:
            return None
        client = _get_web_client(self._conn)
        try:
            result = await client.conversations_history(
                channel=channel_id, latest=thread_ts, oldest=thread_ts,
                inclusive=True, limit=1, include_all_metadata=True,
            )
        except Exception:
            logger.exception(
                "slack: conversations.history lookup failed for %s/%s",
                channel_id, thread_ts,
            )
            return None
        msgs = result.get("messages") or []
        if not msgs:
            return None
        meta = (msgs[0].get("metadata") or {}).get("event_payload") or {}
        if meta.get("kind") not in ("ask_user", "tool_approval"):
            return None
        self._thread_payload_cache[(channel_id, thread_ts)] = meta
        self._lru_evict()
        return meta


__all__ = ["REJECT_MODAL_CALLBACK_ID", "SlackChannelAdapter"]
