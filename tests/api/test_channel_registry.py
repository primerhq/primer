"""Smoke tests for ChannelRegistry — uses NullChannelAdapter via
the factory-registry hook so we don't depend on any real platform."""

from __future__ import annotations

from datetime import datetime, timezone
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
    SlackChannelProviderConfig,
)
from primer.model.provider import SqliteConfig
from primer.model.workspace import Workspace, WorkspaceChannelLink, WorkspaceRuntimeMeta
from primer.storage.sqlite import SqliteStorageProvider


@pytest.fixture(autouse=True)
def _factory_isolation():
    clear_factories_for_tests()
    yield
    clear_factories_for_tests()


def _make_workspace(id: str, channel_id: str | None = None, template_id: str = "t-1", provider_id: str = "p-1") -> Workspace:
    return Workspace(
        id=id,
        template_id=template_id,
        provider_id=provider_id,
        created_at=datetime.now(timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://localhost:5959",
            token=SecretStr("test-token"),
        ),
        reply_binding=WorkspaceChannelLink(channel_id=channel_id) if channel_id else None,
    )


@pytest.mark.asyncio
async def test_for_workspace_returns_adapter_for_linked_channel(tmp_path: Path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    try:
        async def _factory(provider_row, channel_row, inbox, **_kw):
            adapter = NullChannelAdapter()
            await adapter.initialize()
            return adapter
        register_adapter_factory(ChannelProviderType.SLACK, _factory)

        cp_storage = p.get_storage(ChannelProvider)
        c_storage = p.get_storage(Channel)
        ws_storage = p.get_storage(Workspace)
        await cp_storage.create(ChannelProvider(
            id="cp-1", provider=ChannelProviderType.SLACK,
            config=SlackChannelProviderConfig(
            app_token=SecretStr("xapp-test"),
            bot_token=SecretStr("xoxb-test"),
        ),
        ))
        await c_storage.create(Channel(
            id="ch-1", provider_id="cp-1",
            provider=ChannelProviderType.SLACK,
            external_id="C1",
        ))
        await c_storage.create(Channel(
            id="ch-2", provider_id="cp-1",
            provider=ChannelProviderType.SLACK,
            external_id="C2",
        ))
        # ws-1 links to ch-1; ws-2 has no association
        await ws_storage.create(_make_workspace("ws-1", channel_id="ch-1"))
        await ws_storage.create(_make_workspace("ws-2"))

        inbox = ChannelInbox(event_bus=None)
        reg = ChannelRegistry(
            channel_storage=c_storage,
            channel_provider_storage=cp_storage,
            inbox=inbox,
            storage_provider=p,
        )
        try:
            adapters = await reg.for_workspace("ws-1")
            assert len(adapters) == 1
            assert isinstance(adapters[0], NullChannelAdapter)

            # Workspace with no association returns empty list
            no_adapters = await reg.for_workspace("ws-2")
            assert no_adapters == []

            # Unknown workspace returns empty list
            unknown = await reg.for_workspace("ws-unknown")
            assert unknown == []
        finally:
            await reg.aclose()
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_for_workspace_no_storage_provider(tmp_path: Path):
    """for_workspace returns [] when no storage_provider is wired."""
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    try:
        cp_storage = p.get_storage(ChannelProvider)
        c_storage = p.get_storage(Channel)
        inbox = ChannelInbox(event_bus=None)
        reg = ChannelRegistry(
            channel_storage=c_storage,
            channel_provider_storage=cp_storage,
            inbox=inbox,
            # no storage_provider
        )
        try:
            result = await reg.for_workspace("ws-1")
            assert result == []
        finally:
            await reg.aclose()
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_get_adapter_caches_per_channel_id(tmp_path: Path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    try:
        async def _factory(provider_row, channel_row, inbox, **_kw):
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
            id="ch-1", provider_id="cp-1",
            provider=ChannelProviderType.SLACK,
            external_id="C1",
        ))
        inbox = ChannelInbox(event_bus=None)
        reg = ChannelRegistry(
            channel_storage=c_storage,
            channel_provider_storage=cp_storage,
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
