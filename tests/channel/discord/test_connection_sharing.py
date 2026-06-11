"""Shared Discord Client refcounting tests."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

discord = pytest.importorskip("discord")

from primer.channel.discord.connection import _DiscordConnectionRegistry
from primer.model.channel import (
    ChannelProvider, ChannelProviderType, DiscordChannelProviderConfig,
)


def _provider(id_: str) -> ChannelProvider:
    return ChannelProvider(
        id=id_, provider=ChannelProviderType.DISCORD,
        config=DiscordChannelProviderConfig(bot_token=SecretStr("a" * 60)),
    )


@pytest.mark.asyncio
async def test_start_client_logs_in_before_waiting_for_ready():
    # Regression: wait_until_ready on an unlogged-in discord.Client raises
    # "Client has not been properly initialised", so login() must happen
    # first and the gateway loop must run via connect() (not start()).
    from primer.channel.discord.connection import _start_client_as_task

    calls: list[str] = []

    class _FakeClient:
        async def login(self, token):
            calls.append("login")

        async def connect(self):
            calls.append("connect")  # background gateway loop

        async def start(self, token, **kwargs):
            calls.append("start")  # must NOT be used

        async def wait_until_ready(self):
            calls.append("ready")
            if "login" not in calls:
                raise RuntimeError("Client has not been properly initialised")

    task = await _start_client_as_task(_FakeClient(), "tok", ready_wait=2.0)
    assert calls[0] == "login"
    assert "ready" in calls and "start" not in calls
    task.cancel()


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
        "primer.channel.discord.connection._build_client",
        lambda cfg: _FakeClient(),
    )
    # Stub the start-as-task helper so the test doesn't actually
    # try to connect to Discord.
    async def _start_task(client, token, *, ready_wait=1.0): pass
    monkeypatch.setattr(
        "primer.channel.discord.connection._start_client_as_task",
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
