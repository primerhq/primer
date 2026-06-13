"""ChannelRegistry.warm_chat_channels starts bots for channels with
config.chats.enabled=True.

Chat-driven bots are user-initiated, so unlike session channels (warmed by the
first outbound park) they have no other start trigger. warm_chat_channels eagerly
initializes the adapter for each Channel whose chat config is enabled at boot.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from primer.api.registries.channel_registry import ChannelRegistry
from primer.channel.factory import (
    clear_factories_for_tests,
    register_adapter_factory,
)
from primer.channel.inbox import ChannelInbox
from primer.channel.null_adapter import NullChannelAdapter
from primer.model.channel import (
    Channel,
    ChannelProvider,
    ChannelProviderType,
    ChatConfig,
    TelegramChannelConfig,
    TelegramChannelProviderConfig,
)
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


@pytest.fixture(autouse=True)
def _factory_isolation():
    clear_factories_for_tests()
    yield
    clear_factories_for_tests()


@pytest.mark.asyncio
async def test_warm_starts_enabled_chat_adapters(tmp_path: Path):
    started: list[str] = []

    async def _factory(provider_row, channel_row, inbox, **_kw):
        adapter = NullChannelAdapter()
        await adapter.initialize()
        started.append(channel_row.id)
        return adapter

    register_adapter_factory(ChannelProviderType.TELEGRAM, _factory)

    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    cp = p.get_storage(ChannelProvider)
    ch = p.get_storage(Channel)
    await cp.create(ChannelProvider(
        id="cp-1", provider=ChannelProviderType.TELEGRAM,
        config=TelegramChannelProviderConfig(
            bot_token=SecretStr("123456:ABCDEFGHIJKLMNOP"))))
    # ch-1 has chats enabled -> warmed; ch-2 disabled -> skipped.
    await ch.create(Channel(
        id="ch-1", provider_id="cp-1",
        provider=ChannelProviderType.TELEGRAM,
        external_id="555",
        config=TelegramChannelConfig(
            chats=ChatConfig(enabled=True, default_agent="agent-x"),
        ),
    ))
    await ch.create(Channel(
        id="ch-2", provider_id="cp-1",
        provider=ChannelProviderType.TELEGRAM,
        external_id="556",
        config=TelegramChannelConfig(
            chats=ChatConfig(enabled=False),
        ),
    ))

    reg = ChannelRegistry(
        channel_storage=ch,
        channel_provider_storage=cp,
        inbox=ChannelInbox(event_bus=None),
        storage_provider=p,
    )
    try:
        count = await reg.warm_chat_channels()
        assert count == 1
        assert started == ["ch-1"]
    finally:
        await reg.aclose()


@pytest.mark.asyncio
async def test_warm_noop_without_storage_provider(tmp_path: Path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    reg = ChannelRegistry(
        channel_storage=p.get_storage(Channel),
        channel_provider_storage=p.get_storage(ChannelProvider),
        inbox=ChannelInbox(event_bus=None),
    )
    assert await reg.warm_chat_channels() == 0
