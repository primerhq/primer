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
    ChannelAdapter, PromptEnvelope, ResponseEnvelope, session_thread_label,
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
        storage_provider=None,
        event_bus=None,
    ) -> None:
        self._provider = provider
        self._channel = channel
        self._inbox = inbox
        # Chat-surface wiring. Optional so existing callers (session/workspace
        # channels) keep working; the chat dispatch path stays inactive when
        # _sp is None.
        self._sp = storage_provider
        self._bus = event_bus
        self._conn: Any | None = None
        # session_id → root message ts (the per-session conversation thread).
        self._session_threads: dict[str, str] = {}
        # thread root ts → {ws, sid, tcid} for the ask_user awaiting a reply in
        # that thread (sessions park on one prompt at a time).
        self._pending_ask: dict[str, dict[str, str]] = {}

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
        root_ts = await self._session_root_ts(client, envelope.session_id)
        if envelope.kind == "inform":
            await client.chat_postMessage(
                channel=self._channel.external_id,
                thread_ts=root_ts,
                text=envelope.prompt,
            )
            return {"ts": "", "channel": self._channel.external_id, "thread_ts": root_ts}
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
        # Post every prompt into the session's conversation thread.
        body["thread_ts"] = root_ts
        resp = await client.chat_postMessage(**body)
        ts = resp.get("ts", "")
        if envelope.kind == "ask_user":
            self._pending_ask[root_ts] = {
                "ws": envelope.workspace_id,
                "sid": envelope.session_id,
                "tcid": envelope.tool_call_id,
            }
        return {"ts": ts, "channel": resp.get("channel", ""), "thread_ts": root_ts}

    async def post_chat_message(
        self, text: str, *, thread_ts: str | None = None,
    ) -> dict:
        """Outbound chat relay: stream into the thread (native) or post whole."""
        from primer.channel.slack.streaming import stream_or_post
        if self._conn is None:
            raise ProviderError("SlackChannelAdapter used before initialize()")
        client = _get_web_client(self._conn)
        await stream_or_post(
            client=client, channel=self._channel.external_id,
            thread_ts=thread_ts, text=text)
        return {"ok": True}

    async def _session_root_ts(self, client: Any, session_id: str) -> str:
        """Get-or-create the root message ts for this session's thread.

        The first prompt posts a small anchor message to the channel; its ts is
        the thread root every later prompt for the session replies under.
        """
        ts = self._session_threads.get(session_id)
        if ts:
            return ts
        resp = await client.chat_postMessage(
            channel=self._channel.external_id,
            text=f":thread: {session_thread_label(session_id)}",
        )
        ts = resp.get("ts", "")
        self._session_threads[session_id] = ts
        return ts

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

    async def handle_inbound_chat_message(
        self, *, thread_ts: str | None, message_ts: str,
        sender_name: str, text: str,
    ):
        """Multi-type inbound: top-level opens a new thread-chat; an in-thread
        message routes to that thread's chat. The thread id is message_ts on a
        top-level message (Slack threads anchor on the parent ts)."""
        from primer.channel.chat_inbox import ChatResponseInbox
        from primer.channel.chat_router import ChatChannelRouter
        thread_external_id = thread_ts or message_ts
        gate_inbox = ChatResponseInbox(
            storage_provider=self._sp, event_bus=self._bus)
        router = ChatChannelRouter(
            storage_provider=self._sp, event_bus=self._bus, gate_inbox=gate_inbox)
        chat, _created = await router.deliver_message(
            channel_id=self._channel.id, thread_external_id=thread_external_id,
            supports_threads=True, sender_name=sender_name, text=text)
        return chat

    def pending_ask_for_thread(self, thread_ts: str) -> dict[str, str] | None:
        """The ask_user (ws/sid/tcid) awaiting a reply in this thread, if any."""
        return self._pending_ask.get(thread_ts)

    def clear_pending_ask(self, thread_ts: str) -> None:
        self._pending_ask.pop(thread_ts, None)


__all__ = ["REJECT_MODAL_CALLBACK_ID", "SlackChannelAdapter"]
