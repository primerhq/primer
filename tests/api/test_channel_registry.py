"""Smoke tests for ChannelRegistry — uses NullChannelAdapter via
the factory-registry hook so we don't depend on any real platform."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from matrix.api.registries.channel_registry import ChannelRegistry
from matrix.channel.factory import (
    clear_factories_for_tests,
    register_adapter_factory,
)
from matrix.channel.inbox import ChannelInbox
from matrix.channel.null_adapter import NullChannelAdapter
from matrix.model.channel import (
    Channel,
    ChannelProvider,
    ChannelProviderType,
    SlackChannelProviderConfig,
    WorkspaceChannelAssociation,
)
from matrix.model.provider import SqliteConfig
from matrix.storage.sqlite import SqliteStorageProvider


@pytest.fixture(autouse=True)
def _factory_isolation():
    clear_factories_for_tests()
    yield
    clear_factories_for_tests()


@pytest.mark.asyncio
async def test_for_workspace_returns_only_enabled_pairs(tmp_path: Path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    try:
        async def _factory(provider_row, channel_row, inbox):
            adapter = NullChannelAdapter()
            await adapter.initialize()
            return adapter
        register_adapter_factory(ChannelProviderType.SLACK, _factory)

        cp_storage = p.get_storage(ChannelProvider)
        c_storage = p.get_storage(Channel)
        a_storage = p.get_storage(WorkspaceChannelAssociation)
        await cp_storage.create(ChannelProvider(
            id="cp-1", provider=ChannelProviderType.SLACK,
            config=SlackChannelProviderConfig(
            app_token=SecretStr("xapp-test"),
            bot_token=SecretStr("xoxb-test"),
        ),
        ))
        await c_storage.create(Channel(
            id="ch-1", provider_id="cp-1", external_id="C1",
        ))
        await c_storage.create(Channel(
            id="ch-2", provider_id="cp-1", external_id="C2",
        ))
        await a_storage.create(WorkspaceChannelAssociation(
            id="a-1", workspace_id="ws-1", channel_id="ch-1",
            enabled=True,
        ))
        await a_storage.create(WorkspaceChannelAssociation(
            id="a-2", workspace_id="ws-1", channel_id="ch-2",
            enabled=False,
        ))
        inbox = ChannelInbox(event_bus=None)
        reg = ChannelRegistry(
            channel_storage=c_storage,
            channel_provider_storage=cp_storage,
            association_storage=a_storage,
            inbox=inbox,
        )
        try:
            pairs = await reg.for_workspace("ws-1")
            assert len(pairs) == 1
            assert isinstance(pairs[0][0], NullChannelAdapter)
            assert pairs[0][1].channel_id == "ch-1"
        finally:
            await reg.aclose()
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_get_adapter_caches_per_channel_id(tmp_path: Path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    try:
        async def _factory(provider_row, channel_row, inbox):
            adapter = NullChannelAdapter()
            await adapter.initialize()
            return adapter
        register_adapter_factory(ChannelProviderType.SLACK, _factory)

        cp_storage = p.get_storage(ChannelProvider)
        c_storage = p.get_storage(Channel)
        await cp_storage.create(ChannelProvider(
            id="cp-1", provider=ChannelProviderType.SLACK,
            config=SlackChannelProviderConfig(
            app_token=SecretStr("xapp-test"),
            bot_token=SecretStr("xoxb-test"),
        ),
        ))
        await c_storage.create(Channel(
            id="ch-1", provider_id="cp-1", external_id="C1",
        ))
        inbox = ChannelInbox(event_bus=None)
        reg = ChannelRegistry(
            channel_storage=c_storage,
            channel_provider_storage=cp_storage,
            association_storage=p.get_storage(WorkspaceChannelAssociation),
            inbox=inbox,
        )
        try:
            a1 = await reg.get_adapter("ch-1")
            a2 = await reg.get_adapter("ch-1")
            assert a1 is a2
        finally:
            await reg.aclose()
    finally:
        await p.aclose()
