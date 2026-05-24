"""Two adapters under one provider share one Slack connection."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from matrix.channel.slack.connection import _SlackConnectionRegistry
from matrix.model.channel import (
    ChannelProvider, ChannelProviderType, SlackChannelProviderConfig,
)


def _provider(id_: str) -> ChannelProvider:
    return ChannelProvider(
        id=id_,
        provider=ChannelProviderType.SLACK,
        config=SlackChannelProviderConfig(
            app_token=SecretStr("xapp-1-test"),
            bot_token=SecretStr("xoxb-test"),
        ),
    )


@pytest.mark.asyncio
async def test_acquire_returns_same_connection_for_same_provider(monkeypatch):
    started: list[str] = []

    class _FakeApp:
        def __init__(self, token: str): self._token = token
        async def start_async(self): started.append("start")
        async def close_async(self): started.append("close")

    monkeypatch.setattr(
        "matrix.channel.slack.connection._build_app_and_handler",
        lambda cfg: _FakeApp(token=cfg.bot_token.get_secret_value()),
    )

    reg = _SlackConnectionRegistry()
    c1 = await reg.acquire(_provider("cp-1"))
    c2 = await reg.acquire(_provider("cp-1"))
    assert c1 is c2
    assert started.count("start") == 1  # started only once
    await reg.release(_provider("cp-1"))  # ref still held
    await reg.release(_provider("cp-1"))  # last ref drops
    assert "close" in started


@pytest.mark.asyncio
async def test_different_providers_get_distinct_connections(monkeypatch):
    class _FakeApp:
        async def start_async(self): pass
        async def close_async(self): pass
    monkeypatch.setattr(
        "matrix.channel.slack.connection._build_app_and_handler",
        lambda cfg: _FakeApp(),
    )

    reg = _SlackConnectionRegistry()
    a = await reg.acquire(_provider("cp-1"))
    b = await reg.acquire(_provider("cp-2"))
    assert a is not b
    await reg.release(_provider("cp-1"))
    await reg.release(_provider("cp-2"))
