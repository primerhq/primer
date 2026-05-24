"""Shared Discord Client refcounting tests."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

discord = pytest.importorskip("discord")

from matrix.channel.discord.connection import _DiscordConnectionRegistry
from matrix.model.channel import (
    ChannelProvider, ChannelProviderType, DiscordChannelProviderConfig,
)


def _provider(id_: str) -> ChannelProvider:
    return ChannelProvider(
        id=id_, provider=ChannelProviderType.DISCORD,
        config=DiscordChannelProviderConfig(bot_token=SecretStr("a" * 60)),
    )


@pytest.mark.asyncio
async def test_acquire_returns_same_client(monkeypatch):
    starts: list[str] = []
    closes: list[str] = []

    class _FakeClient:
        def __init__(self, *_, **__): pass
        async def start(self, token, **kwargs): starts.append("start")
        async def close(self): closes.append("close")
        async def login(self, token): pass
        async def wait_until_ready(self): pass
        @property
        def user(self): return type("U", (), {"id": 42})()

    monkeypatch.setattr(
        "matrix.channel.discord.connection._build_client",
        lambda cfg: _FakeClient(),
    )
    # Stub the start-as-task helper so the test doesn't actually
    # try to connect to Discord.
    async def _start_task(client, token, *, ready_wait=1.0): pass
    monkeypatch.setattr(
        "matrix.channel.discord.connection._start_client_as_task",
        _start_task,
    )
    reg = _DiscordConnectionRegistry()
    a = await reg.acquire(_provider("cp-1"))
    b = await reg.acquire(_provider("cp-1"))
    assert a is b
    await reg.release(_provider("cp-1"))
    assert closes == []
    await reg.release(_provider("cp-1"))
    assert closes == ["close"]
