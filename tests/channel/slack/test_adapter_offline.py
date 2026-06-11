"""Network-mocked unit tests for SlackChannelAdapter."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from primer.channel.adapter import PromptEnvelope, ResponseEnvelope
from primer.channel.inbox import ChannelInbox
from primer.channel.slack.adapter import SlackChannelAdapter
from primer.channel.slack.connection import SLACK_CONNECTIONS
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
    SlackChannelProviderConfig,
)


class _CapturingInbox(ChannelInbox):
    def __init__(self) -> None:  # bypass parent init
        self.received: list[ResponseEnvelope] = []

    async def handle_response(self, env: ResponseEnvelope) -> None:
        self.received.append(env)


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._ts = 1000

    async def chat_postMessage(self, **body) -> dict:
        self.calls.append(("chat.postMessage", body))
        self._ts += 1
        return {"ok": True, "ts": f"{self._ts}.0001", "channel": body["channel"]}

    async def auth_test(self) -> dict:
        self.calls.append(("auth.test", {}))
        return {"ok": True, "user": "bot", "team": "T"}

    async def conversations_info(self, channel: str) -> dict:
        self.calls.append(("conversations.info", {"channel": channel}))
        return {"ok": True, "channel": {"id": channel, "name": "general"}}


class _StubConnection:
    def __init__(self) -> None:
        self.app = type("App", (), {})()
        self.client = _StubClient()

    async def start_async(self): pass
    async def close_async(self): pass


def _provider() -> ChannelProvider:
    return ChannelProvider(
        id="cp-1", provider=ChannelProviderType.SLACK,
        config=SlackChannelProviderConfig(
            app_token=SecretStr("xapp-1-test"),
            bot_token=SecretStr("xoxb-test"),
        ),
    )


def _channel() -> Channel:
    return Channel(id="ch-1", provider_id="cp-1", external_id="C01")


@pytest.mark.asyncio
async def test_verify_calls_auth_test_and_conversations_info(monkeypatch):
    monkeypatch.setattr(
        "primer.channel.slack.adapter._get_web_client",
        lambda conn: conn.client,
    )
    conn = _StubConnection()
    async def _acquire(provider):
        return conn
    async def _release(provider): pass
    monkeypatch.setattr(SLACK_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(SLACK_CONNECTIONS, "release", _release)
    inbox = _CapturingInbox()
    adapter = SlackChannelAdapter(
        provider=_provider(), channel=_channel(), inbox=inbox,
    )
    await adapter.initialize()
    try:
        await adapter.verify()
    finally:
        await adapter.aclose()
    kinds = [c[0] for c in conn.client.calls]
    assert "auth.test" in kinds and "conversations.info" in kinds


@pytest.mark.asyncio
async def test_post_prompt_ask_user_calls_chat_postMessage(monkeypatch):
    conn = _StubConnection()
    monkeypatch.setattr(
        "primer.channel.slack.adapter._get_web_client",
        lambda conn: conn.client,
    )
    async def _acquire(provider): return conn
    async def _release(provider): pass
    monkeypatch.setattr(SLACK_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(SLACK_CONNECTIONS, "release", _release)
    inbox = _CapturingInbox()
    adapter = SlackChannelAdapter(
        provider=_provider(), channel=_channel(), inbox=inbox,
    )
    await adapter.initialize()
    try:
        await adapter.post_prompt(PromptEnvelope(
            kind="ask_user", workspace_id="ws", session_id="s", tool_call_id="tc",
            prompt="ping?", response_schema=None, choices=None,
            timeout_at_iso=None,
        ))
    finally:
        await adapter.aclose()
    posts = [b for k, b in conn.client.calls if k == "chat.postMessage"]
    # Two posts: the session-thread anchor, then the prompt threaded under it.
    assert len(posts) == 2
    anchor, prompt = posts
    assert "Agent session s" in anchor["text"] and "thread_ts" not in anchor
    assert prompt["channel"] == "C01"
    assert prompt["thread_ts"] == anchor["ts"] if "ts" in anchor else True
    assert prompt["thread_ts"] == "1001.0001"  # anchor's returned ts
    assert prompt["metadata"]["event_payload"]["tcid"] == "tc"
    # The ask is now pending a reply on the thread root.
    assert adapter.pending_ask_for_thread("1001.0001") == {
        "ws": "ws", "sid": "s", "tcid": "tc",
    }


@pytest.mark.asyncio
async def test_post_prompt_reuses_one_thread_per_session(monkeypatch):
    conn = _StubConnection()
    monkeypatch.setattr(
        "primer.channel.slack.adapter._get_web_client", lambda conn: conn.client,
    )
    async def _acquire(provider): return conn
    async def _release(provider): pass
    monkeypatch.setattr(SLACK_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(SLACK_CONNECTIONS, "release", _release)
    adapter = SlackChannelAdapter(
        provider=_provider(), channel=_channel(), inbox=_CapturingInbox(),
    )
    await adapter.initialize()
    try:
        await adapter.post_prompt(PromptEnvelope(
            kind="ask_user", workspace_id="ws", session_id="s", tool_call_id="t1",
            prompt="q1", response_schema=None, choices=None, timeout_at_iso=None,
        ))
        await adapter.post_prompt(PromptEnvelope(
            kind="tool_approval", workspace_id="ws", session_id="s", tool_call_id="t2",
            prompt="approve?", response_schema=None, choices=["Approve", "Reject"],
            timeout_at_iso=None, tool_name="workspace__write", tool_args={"x": 1},
        ))
    finally:
        await adapter.aclose()
    posts = [b for k, b in conn.client.calls if k == "chat.postMessage"]
    # One anchor + two prompts (not two anchors).
    assert len(posts) == 3
    anchors = [p for p in posts if "thread_ts" not in p]
    assert len(anchors) == 1
    threaded = [p for p in posts if "thread_ts" in p]
    assert {p["thread_ts"] for p in threaded} == {"1001.0001"}


@pytest.mark.asyncio
async def test_handle_decision_publishes_to_inbox(monkeypatch):
    conn = _StubConnection()
    monkeypatch.setattr(
        "primer.channel.slack.adapter._get_web_client",
        lambda conn: conn.client,
    )
    async def _acquire(provider): return conn
    async def _release(provider): pass
    monkeypatch.setattr(SLACK_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(SLACK_CONNECTIONS, "release", _release)
    inbox = _CapturingInbox()
    adapter = SlackChannelAdapter(
        provider=_provider(), channel=_channel(), inbox=inbox,
    )
    await adapter.initialize()
    try:
        await adapter._handle_decision(
            ws="ws", sid="s", tcid="tc",
            decision="approved", reason=None, slack_user_id="U1",
        )
    finally:
        await adapter.aclose()
    assert len(inbox.received) == 1
    env = inbox.received[0]
    assert env.kind == "tool_approval"
    assert env.decision == "approved"


@pytest.mark.asyncio
async def test_handle_text_reply_publishes_ask_user(monkeypatch):
    conn = _StubConnection()
    monkeypatch.setattr(
        "primer.channel.slack.adapter._get_web_client",
        lambda conn: conn.client,
    )
    async def _acquire(provider): return conn
    async def _release(provider): pass
    monkeypatch.setattr(SLACK_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(SLACK_CONNECTIONS, "release", _release)
    inbox = _CapturingInbox()
    adapter = SlackChannelAdapter(
        provider=_provider(), channel=_channel(), inbox=inbox,
    )
    await adapter.initialize()
    try:
        await adapter._handle_text_reply(
            ws="ws", sid="s", tcid="tc",
            text="here is my answer", slack_user_id="U1",
        )
    finally:
        await adapter.aclose()
    assert len(inbox.received) == 1
    env = inbox.received[0]
    assert env.kind == "ask_user"
    assert env.response == "here is my answer"
