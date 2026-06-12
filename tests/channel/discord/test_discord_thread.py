"""DiscordChannelAdapter._resolve_chat_thread opens a thread off the anchor
message when none exists yet, and reuses an existing thread otherwise."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from primer.channel.discord.adapter import DiscordChannelAdapter
from primer.model.channel import (
    Channel,
    ChannelProvider,
    ChannelProviderType,
    DiscordChannelProviderConfig,
)


class _Thread:
    def __init__(self, tid: int):
        self.id = tid
        self.sent: list[str] = []

    async def send(self, *, content: str):
        self.sent.append(content)
        return object()


class _Message:
    def __init__(self, mid: int):
        self.id = mid
        self.created_thread: _Thread | None = None

    async def create_thread(self, *, name: str, auto_archive_duration: int):
        # Discord gives the thread the anchor message's id.
        self.created_thread = _Thread(self.id)
        return self.created_thread


class _Channel:
    def __init__(self, cid: int):
        self.id = cid
        self._messages: dict[int, _Message] = {}

    def add_message(self, mid: int) -> _Message:
        m = _Message(mid)
        self._messages[mid] = m
        return m

    async def fetch_message(self, mid: int):
        return self._messages[mid]


class _Client:
    def __init__(self, parent: _Channel):
        self._parent = parent
        self._channels: dict[int, object] = {parent.id: parent}

    def get_channel(self, cid: int):
        return self._channels.get(cid)

    async def fetch_channel(self, cid: int):
        return self._channels.get(cid)

    def register(self, obj) -> None:
        self._channels[obj.id] = obj


def _adapter(client) -> DiscordChannelAdapter:
    cp = ChannelProvider(
        id="cp-1", provider=ChannelProviderType.DISCORD,
        config=DiscordChannelProviderConfig(bot_token=SecretStr("x" * 40)))
    ch = Channel(id="ch-1", provider_id="cp-1", external_id="9001")
    a = DiscordChannelAdapter(provider=cp, channel=ch, inbox=None)
    a._client = client
    return a


@pytest.mark.asyncio
async def test_creates_thread_off_anchor_message_when_none_exists():
    parent = _Channel(9001)
    parent.add_message(1700)  # the top-level (anchor) message
    client = _Client(parent)
    a = _adapter(client)

    target = await a._resolve_chat_thread("1700")
    # A thread was opened off the anchor message, with the message's id.
    assert isinstance(target, _Thread)
    assert target.id == 1700


@pytest.mark.asyncio
async def test_reuses_existing_thread():
    parent = _Channel(9001)
    client = _Client(parent)
    existing = _Thread(1700)
    client.register(existing)  # thread already exists with id 1700
    a = _adapter(client)

    target = await a._resolve_chat_thread("1700")
    assert target is existing


@pytest.mark.asyncio
async def test_none_thread_ts_returns_parent_channel():
    parent = _Channel(9001)
    client = _Client(parent)
    a = _adapter(client)

    target = await a._resolve_chat_thread(None)
    assert target is parent
