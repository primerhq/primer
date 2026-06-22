"""Regression: a channel / channel-provider config edit invalidates the warm
adapter so the next access rebuilds it lazily against the new config.

Without invalidation, the registry caches the adapter built at first touch and
keeps serving its stale warm gateway (Discord/Slack WS, Telegram poller) for the
life of the process -- a "config change needs a restart" bug. These tests drive
the registry the way the channels.py on_update / on_delete hooks do.
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
    SlackChannelProviderConfig,
)
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


@pytest.fixture(autouse=True)
def _factory_isolation():
    clear_factories_for_tests()
    yield
    clear_factories_for_tests()


class _RecordingAdapter(NullChannelAdapter):
    """Captures the external_id of the Channel row it was built from so a test
    can prove a rebuilt adapter reflects the updated config."""

    def __init__(self, external_id: str) -> None:
        super().__init__()
        self.built_external_id = external_id


def _register_recording_factory() -> None:
    async def _factory(provider_row, channel_row, inbox, **_kw):
        adapter = _RecordingAdapter(channel_row.external_id)
        await adapter.initialize()
        return adapter

    register_adapter_factory(ChannelProviderType.SLACK, _factory)


async def _seed(p: SqliteStorageProvider, *, external_id: str = "C1") -> None:
    cp_storage = p.get_storage(ChannelProvider)
    c_storage = p.get_storage(Channel)
    await cp_storage.create(ChannelProvider(
        id="cp-1", provider=ChannelProviderType.SLACK,
        config=SlackChannelProviderConfig(
            app_token=SecretStr("xapp-v1"),
            bot_token=SecretStr("xoxb-v1"),
        ),
    ))
    await c_storage.create(Channel(
        id="ch-1", provider_id="cp-1",
        provider=ChannelProviderType.SLACK,
        external_id=external_id,
    ))


def _build_registry(p: SqliteStorageProvider) -> ChannelRegistry:
    return ChannelRegistry(
        channel_storage=p.get_storage(Channel),
        channel_provider_storage=p.get_storage(ChannelProvider),
        inbox=ChannelInbox(event_bus=None),
        storage_provider=p,
    )


@pytest.mark.asyncio
async def test_channel_update_invalidates_and_rebuilds_with_new_config(
    tmp_path: Path,
):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    try:
        _register_recording_factory()
        await _seed(p, external_id="C1")
        reg = _build_registry(p)
        try:
            # First touch warms the adapter against config v1 ("C1").
            a1 = await reg.get_adapter("ch-1")
            assert isinstance(a1, _RecordingAdapter)
            assert a1.built_external_id == "C1"
            assert a1 is await reg.get_adapter("ch-1")  # cached

            # Operator edits the channel config (external_id C1 -> C2).
            c_storage = p.get_storage(Channel)
            row = await c_storage.get("ch-1")
            await c_storage.update(row.model_copy(update={"external_id": "C2"}))

            # The on_update hook calls invalidate(channel_id=...): the warm
            # adapter is cleanly closed and dropped from the cache.
            await reg.invalidate(channel_id="ch-1")
            assert a1._closed is True

            # Next access rebuilds lazily against the NEW config (no stale warm).
            a2 = await reg.get_adapter("ch-1")
            assert a2 is not a1
            assert a2.built_external_id == "C2"
        finally:
            await reg.aclose()
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_channel_delete_invalidation_closes_adapter(tmp_path: Path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    try:
        _register_recording_factory()
        await _seed(p)
        reg = _build_registry(p)
        try:
            a1 = await reg.get_adapter("ch-1")
            await reg.invalidate(channel_id="ch-1")
            assert a1._closed is True
            # Invalidating an unknown / already-dropped id is a no-op.
            await reg.invalidate(channel_id="ch-1")
            await reg.invalidate(channel_id="does-not-exist")
        finally:
            await reg.aclose()
    finally:
        await p.aclose()


class _FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def invalidate(self, *, channel_id):
        self.calls.append(channel_id)


class _FakeRequest:
    def __init__(self, registry) -> None:
        self.app = type("_App", (), {"state": type("_S", (), {})()})()
        self.app.state.channel_registry = registry


@pytest.mark.asyncio
async def test_channel_router_hooks_invalidate_the_registry():
    """The on_update / on_delete hooks wired into the channel router target the
    single edited channel; the provider hook flushes the whole cache."""
    from primer.api.routers.channels import (
        _invalidate_channel,
        _invalidate_provider_channels,
    )

    reg = _FakeRegistry()
    req = _FakeRequest(reg)
    await _invalidate_channel("ch-1", req)
    await _invalidate_provider_channels("cp-1", req)
    assert reg.calls == ["ch-1", None]


@pytest.mark.asyncio
async def test_invalidation_hooks_noop_without_registry():
    """No channel_registry on app.state (minimal apps) -> hooks are a no-op."""
    from primer.api.routers.channels import (
        _invalidate_channel,
        _invalidate_provider_channels,
    )

    req = type("_R", (), {})()
    req.app = type("_App", (), {"state": type("_S", (), {})()})()
    # No channel_registry attribute set -> getattr(..., None) returns None.
    await _invalidate_channel("ch-1", req)
    await _invalidate_provider_channels("cp-1", req)


@pytest.mark.asyncio
async def test_provider_invalidation_flushes_all_channels(tmp_path: Path):
    """A ChannelProvider edit (rotated token) flushes every warm adapter via
    invalidate(channel_id=None), the way the provider router on_update does."""
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    try:
        _register_recording_factory()
        await _seed(p)
        c_storage = p.get_storage(Channel)
        await c_storage.create(Channel(
            id="ch-2", provider_id="cp-1",
            provider=ChannelProviderType.SLACK,
            external_id="C-OTHER",
        ))
        reg = _build_registry(p)
        try:
            a1 = await reg.get_adapter("ch-1")
            a2 = await reg.get_adapter("ch-2")

            # Provider config changed -> flush the whole cache.
            await reg.invalidate(channel_id=None)
            assert a1._closed is True
            assert a2._closed is True

            # Both rebuild fresh on next use.
            assert await reg.get_adapter("ch-1") is not a1
            assert await reg.get_adapter("ch-2") is not a2
        finally:
            await reg.aclose()
    finally:
        await p.aclose()
