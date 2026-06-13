"""Offline unit tests for DiscordChannelAdapter."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

discord = pytest.importorskip("discord")

from primer.channel.adapter import PromptEnvelope, ResponseEnvelope
from primer.channel.inbox import ChannelInbox
from primer.channel.discord.adapter import DiscordChannelAdapter
from primer.channel.discord.connection import DISCORD_CONNECTIONS
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
    DiscordChannelProviderConfig,
)


class _CapturingInbox(ChannelInbox):
    def __init__(self) -> None:
        self.received: list[ResponseEnvelope] = []
    async def handle_response(self, env: ResponseEnvelope) -> None:
        self.received.append(env)


class _StubThread:
    def __init__(self, tid: int):
        self.id = tid
        self.sent: list[dict[str, Any]] = []
    async def send(self, content=None, view=None, **kwargs):
        self.sent.append({"content": content, "view": view})
        return _StubMessage(mid=self.id + 100)


class _StubMessage:
    def __init__(self, mid: int, client=None):
        self.id = mid
        self.content = ""
        self._client = client
    async def create_thread(self, *, name, auto_archive_duration):
        th = _StubThread(self.id + 1)
        if self._client is not None:
            self._client.threads[th.id] = th
        return th


class _StubChannel:
    def __init__(self, cid: int, client):
        self.id = cid
        self.sent: list[dict[str, Any]] = []
        self._client = client
    async def send(self, content=None, view=None, **kwargs):
        self.sent.append({"content": content, "view": view})
        return _StubMessage(mid=999, client=self._client)


class _StubClient:
    def __init__(self) -> None:
        self.threads: dict[int, _StubThread] = {}
        self.channel = _StubChannel(cid=12345, client=self)
    def get_channel(self, cid: int):
        if cid == self.channel.id:
            return self.channel
        return self.threads.get(cid)
    async def fetch_channel(self, cid: int):
        if cid == self.channel.id:
            return self.channel
        return self.threads.get(cid)
    @property
    def user(self):
        return type("U", (), {"id": 42})()


def _provider() -> ChannelProvider:
    return ChannelProvider(
        id="cp-1", provider=ChannelProviderType.DISCORD,
        config=DiscordChannelProviderConfig(bot_token=SecretStr("a" * 60)),
    )


def _channel() -> Channel:
    return Channel(id="ch-1", provider_id="cp-1",
                   provider=ChannelProviderType.DISCORD, external_id="12345")


@pytest.mark.asyncio
async def test_verify_resolves_channel(monkeypatch):
    client = _StubClient()
    async def _acquire(_): return client
    async def _release(_): pass
    monkeypatch.setattr(DISCORD_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(DISCORD_CONNECTIONS, "release", _release)
    inbox = _CapturingInbox()
    adapter = DiscordChannelAdapter(
        provider=_provider(), channel=_channel(), inbox=inbox,
    )
    await adapter.initialize()
    try:
        await adapter.verify()
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_post_tool_approval_attaches_view(monkeypatch):
    client = _StubClient()
    async def _acquire(_): return client
    async def _release(_): pass
    monkeypatch.setattr(DISCORD_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(DISCORD_CONNECTIONS, "release", _release)
    inbox = _CapturingInbox()
    adapter = DiscordChannelAdapter(
        provider=_provider(), channel=_channel(), inbox=inbox,
    )
    await adapter.initialize()
    try:
        await adapter.post_prompt(PromptEnvelope(
            kind="tool_approval", workspace_id="ws", session_id="s",
            tool_call_id="tc", prompt="?", response_schema=None,
            choices=["Approve", "Reject"], timeout_at_iso=None,
        ))
    finally:
        await adapter.aclose()
    # The channel got the session-thread anchor (no view); the approval with
    # its buttons went into the thread.
    assert len(client.channel.sent) == 1
    assert client.channel.sent[0]["view"] is None
    assert "Agent session s" in (client.channel.sent[0]["content"] or "")
    thread = client.threads[1000]  # anchor msg id 999 -> thread id 1000
    assert len(thread.sent) == 1
    assert thread.sent[0]["view"] is not None  # ApprovalView attached in-thread


def test_format_approval_content_uses_tool_name_and_pretty_args():
    from primer.channel.discord.adapter import format_approval_content
    content = format_approval_content(PromptEnvelope(
        kind="tool_approval", workspace_id="ws", session_id="s",
        tool_call_id="tc", prompt="Approve workspace__write({...})?",
        response_schema=None, choices=["Approve", "Reject"],
        timeout_at_iso=None, tool_name="workspace__write",
        tool_args={"path": "hello.txt", "content": "hi"},
    ))
    assert "**Tool:** `workspace__write`" in content
    assert '"path": "hello.txt"' in content       # pretty JSON
    assert "```json" in content                    # fenced code block
    assert "Approve workspace__write({" not in content  # not the raw repr


@pytest.mark.asyncio
async def test_post_inform_goes_to_thread_without_buttons(monkeypatch):
    client = _StubClient()
    async def _acquire(_): return client
    async def _release(_): pass
    monkeypatch.setattr(DISCORD_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(DISCORD_CONNECTIONS, "release", _release)
    adapter = DiscordChannelAdapter(provider=_provider(), channel=_channel(), inbox=_CapturingInbox())
    await adapter.initialize()
    try:
        await adapter.post_prompt(PromptEnvelope(
            kind="inform", workspace_id="ws", session_id="s", tool_call_id="",
            prompt="status update", response_schema=None, choices=None, timeout_at_iso=None))
    finally:
        await adapter.aclose()
    thread = client.threads[1000]
    assert thread.sent[-1]["content"] == "status update"
    assert thread.sent[-1]["view"] is None
    # No session correlation cached in memory (inform gates don't park).
    assert not hasattr(adapter, "_pending_ask")


@pytest.mark.asyncio
async def test_post_ask_user_creates_thread_and_caches_ids(monkeypatch):
    client = _StubClient()
    async def _acquire(_): return client
    async def _release(_): pass
    monkeypatch.setattr(DISCORD_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(DISCORD_CONNECTIONS, "release", _release)
    inbox = _CapturingInbox()
    adapter = DiscordChannelAdapter(
        provider=_provider(), channel=_channel(), inbox=inbox,
    )
    await adapter.initialize()
    try:
        await adapter.post_prompt(PromptEnvelope(
            kind="ask_user", workspace_id="ws", session_id="s",
            tool_call_id="tc", prompt="hi?", response_schema=None,
            choices=None, timeout_at_iso=None,
        ))
        # Anchor msg id 999 -> thread id 1000; ask prompt sent into the thread.
        assert adapter._session_threads["s"] == 1000
        assert client.threads[1000].sent[0]["content"] == "hi?"
        # No in-memory dict: correlation is handled by the persistent store.
        assert not hasattr(adapter, "_pending_ask")
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_post_reuses_one_thread_per_session(monkeypatch):
    client = _StubClient()
    async def _acquire(_): return client
    async def _release(_): pass
    monkeypatch.setattr(DISCORD_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(DISCORD_CONNECTIONS, "release", _release)
    adapter = DiscordChannelAdapter(
        provider=_provider(), channel=_channel(), inbox=_CapturingInbox(),
    )
    await adapter.initialize()
    try:
        await adapter.post_prompt(PromptEnvelope(
            kind="ask_user", workspace_id="ws", session_id="s",
            tool_call_id="t1", prompt="q1", response_schema=None,
            choices=None, timeout_at_iso=None,
        ))
        await adapter.post_prompt(PromptEnvelope(
            kind="tool_approval", workspace_id="ws", session_id="s",
            tool_call_id="t2", prompt="approve?", response_schema=None,
            choices=["Approve", "Reject"], timeout_at_iso=None,
            tool_name="workspace__write", tool_args={"x": 1},
        ))
    finally:
        await adapter.aclose()
    # Exactly one anchor in the channel; both prompts in the single thread.
    assert len(client.channel.sent) == 1
    assert len(client.threads[1000].sent) == 2


@pytest.mark.asyncio
async def test_handle_decision_publishes(monkeypatch):
    client = _StubClient()
    async def _acquire(_): return client
    async def _release(_): pass
    monkeypatch.setattr(DISCORD_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(DISCORD_CONNECTIONS, "release", _release)
    inbox = _CapturingInbox()
    adapter = DiscordChannelAdapter(
        provider=_provider(), channel=_channel(), inbox=inbox,
    )
    await adapter.initialize()
    try:
        await adapter._handle_decision(
            workspace_id="ws", session_id="s", tool_call_id="tc",
            decision="approved", reason=None, discord_user_id=1234,
        )
    finally:
        await adapter.aclose()
    assert len(inbox.received) == 1
    assert inbox.received[0].decision == "approved"
