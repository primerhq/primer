"""Offline unit tests for DiscordChannelAdapter."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

discord = pytest.importorskip("discord")

from matrix.channel.adapter import PromptEnvelope, ResponseEnvelope
from matrix.channel.inbox import ChannelInbox
from matrix.channel.discord.adapter import DiscordChannelAdapter
from matrix.channel.discord.connection import DISCORD_CONNECTIONS
from matrix.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
    DiscordChannelProviderConfig,
)


class _CapturingInbox(ChannelInbox):
    def __init__(self) -> None:
        self.received: list[ResponseEnvelope] = []
    async def handle_response(self, env: ResponseEnvelope) -> None:
        self.received.append(env)


class _StubMessage:
    def __init__(self, mid: int):
        self.id = mid
        self.content = ""
    async def create_thread(self, *, name, auto_archive_duration):
        return type("T", (), {
            "id": self.id + 1,
            "send": (lambda **kwargs: _async_none()),
        })()


def _async_none():
    async def _():
        return None
    return _()


class _StubChannel:
    def __init__(self, cid: int):
        self.id = cid
        self.sent: list[dict[str, Any]] = []
    async def send(self, content=None, view=None, **kwargs):
        self.sent.append({"content": content, "view": view})
        return _StubMessage(mid=999)


class _StubClient:
    def __init__(self) -> None:
        self.channel = _StubChannel(cid=12345)
    def get_channel(self, cid: int):
        return self.channel if cid == self.channel.id else None
    async def fetch_channel(self, cid: int):
        return self.channel
    @property
    def user(self):
        return type("U", (), {"id": 42})()


def _provider() -> ChannelProvider:
    return ChannelProvider(
        id="cp-1", provider=ChannelProviderType.DISCORD,
        config=DiscordChannelProviderConfig(bot_token=SecretStr("a" * 60)),
    )


def _channel() -> Channel:
    return Channel(id="ch-1", provider_id="cp-1", external_id="12345")


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
    assert len(client.channel.sent) == 1
    sent = client.channel.sent[0]
    assert sent["view"] is not None  # ApprovalView attached


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
        # Thread id = parent message id + 1 in our stub.
        assert "1000" in adapter._thread_to_ids
        assert adapter._thread_to_ids["1000"] == {
            "workspace_id": "ws", "session_id": "s", "tool_call_id": "tc",
        }
    finally:
        await adapter.aclose()


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
