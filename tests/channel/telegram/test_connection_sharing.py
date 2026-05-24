"""Multiple adapters under one provider share one PTB Application."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from matrix.channel.telegram.connection import _TelegramConnectionRegistry
from matrix.model.channel import (
    ChannelProvider, ChannelProviderType, TelegramChannelProviderConfig,
)


def _provider(id_: str) -> ChannelProvider:
    return ChannelProvider(
        id=id_, provider=ChannelProviderType.TELEGRAM,
        config=TelegramChannelProviderConfig(
            bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyz123456"),
        ),
    )


@pytest.mark.asyncio
async def test_shared_application_refcounted(monkeypatch):
    starts: list[str] = []
    stops:  list[str] = []

    class _FakeApp:
        async def initialize(self): starts.append("init")
        async def start(self): starts.append("start")
        async def stop(self): stops.append("stop")
        async def shutdown(self): stops.append("shutdown")
        # PTB Application also has updater attribute; we don't poll in tests.
        class _U:
            async def start_polling(self, **_): pass
            async def stop(self): pass
        updater = _U()

    monkeypatch.setattr(
        "matrix.channel.telegram.connection._build_application",
        lambda cfg: _FakeApp(),
    )
    reg = _TelegramConnectionRegistry()
    a = await reg.acquire(_provider("cp-1"))
    b = await reg.acquire(_provider("cp-1"))
    assert a is b
    assert starts.count("start") == 1
    await reg.release(_provider("cp-1"))
    assert stops == []
    await reg.release(_provider("cp-1"))
    assert "stop" in stops
