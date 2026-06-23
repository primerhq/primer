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
    ChatConfig,
    SlackChannelConfig,
    SlackChannelProviderConfig,
)
from primer.model.provider import SqliteConfig
from primer.model.scheduler import RuntimeMode
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


# ---------------------------------------------------------------------------
# GAP-10: live re-warm of the inbound chat adapter when chats are enabled on a
# channel update. Enabling chats on an existing channel must NOT need a server
# restart: a chat is user-initiated, so warm_chat_channels (boot only) was the
# sole inbound trigger. The UPDATE hook now re-warms it live.
# ---------------------------------------------------------------------------


async def _seed_with_chats(
    p: SqliteStorageProvider, *, enabled: bool,
) -> None:
    """Seed cp-1 + ch-1 (Slack) with chats enabled/disabled."""
    cp_storage = p.get_storage(ChannelProvider)
    c_storage = p.get_storage(Channel)
    await cp_storage.create(ChannelProvider(
        id="cp-1", provider=ChannelProviderType.SLACK,
        config=SlackChannelProviderConfig(
            app_token=SecretStr("xapp-v1"),
            bot_token=SecretStr("xoxb-v1"),
        ),
    ))
    chats = (
        ChatConfig(enabled=True, default_agent="agent-x")
        if enabled
        else ChatConfig(enabled=False)
    )
    await c_storage.create(Channel(
        id="ch-1", provider_id="cp-1",
        provider=ChannelProviderType.SLACK,
        external_id="C1",
        config=SlackChannelConfig(chats=chats),
    ))


@pytest.mark.asyncio
async def test_rewarm_if_chat_enabled_builds_adapter(tmp_path: Path):
    """rewarm_if_chat_enabled brings the inbound gateway online when the
    persisted row has chats enabled."""
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    try:
        _register_recording_factory()
        await _seed_with_chats(p, enabled=True)
        reg = _build_registry(p)
        try:
            assert reg.peek_adapter("ch-1") is None  # dark before re-warm
            warmed = await reg.rewarm_if_chat_enabled("ch-1")
            assert warmed is True
            # The inbound adapter is now live (cached) without any outbound park.
            assert reg.peek_adapter("ch-1") is not None
        finally:
            await reg.aclose()
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_rewarm_if_chat_disabled_is_noop(tmp_path: Path):
    """rewarm_if_chat_enabled does NOT open a gateway for a chats-disabled
    channel (and an unknown id is a safe no-op)."""
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    try:
        _register_recording_factory()
        await _seed_with_chats(p, enabled=False)
        reg = _build_registry(p)
        try:
            assert await reg.rewarm_if_chat_enabled("ch-1") is False
            assert reg.peek_adapter("ch-1") is None
            # Unknown id never raises.
            assert await reg.rewarm_if_chat_enabled("nope") is False
        finally:
            await reg.aclose()
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_rewarm_noop_without_storage_provider():
    """No storage_provider wired (minimal apps) -> re-warm is a no-op."""
    reg = ChannelRegistry(
        channel_storage=None,  # type: ignore[arg-type]
        channel_provider_storage=None,  # type: ignore[arg-type]
        inbox=ChannelInbox(event_bus=None),
    )
    assert await reg.rewarm_if_chat_enabled("ch-1") is False


class _StubRegistry:
    """Records invalidate / rewarm calls so the router-hook tests can assert
    ordering and gating without a live adapter."""

    def __init__(self) -> None:
        self.invalidated: list[str | None] = []
        self.rewarmed: list[str] = []

    async def invalidate(self, *, channel_id):
        self.invalidated.append(channel_id)

    async def rewarm_if_chat_enabled(self, channel_id: str) -> bool:
        self.rewarmed.append(channel_id)
        return True


class _StubRequest:
    def __init__(self, registry, *, runtime_mode=None) -> None:
        self.app = type("_App", (), {"state": type("_S", (), {})()})()
        self.app.state.channel_registry = registry
        if runtime_mode is not None:
            self.app.state.config = type(
                "_Cfg", (), {"runtime_mode": runtime_mode},
            )()


@pytest.mark.asyncio
async def test_update_hook_invalidates_then_rewarms_live():
    """The channel UPDATE hook flushes the stale adapter THEN re-warms the
    channel live (in that order) so enabling chats needs no restart."""
    from primer.api.routers.channels import _invalidate_and_rewarm_channel

    reg = _StubRegistry()
    req = _StubRequest(reg, runtime_mode=RuntimeMode.API)
    await _invalidate_and_rewarm_channel("ch-1", req)
    # Invalidate runs before re-warm (drop stale gateway, then rebuild fresh).
    assert reg.invalidated == ["ch-1"]
    assert reg.rewarmed == ["ch-1"]


@pytest.mark.asyncio
async def test_update_hook_does_not_rewarm_in_worker_only_mode():
    """A worker-only process must not open a competing inbound gateway: the
    UPDATE hook still invalidates but skips the live re-warm."""
    from primer.api.routers.channels import _invalidate_and_rewarm_channel

    reg = _StubRegistry()
    req = _StubRequest(reg, runtime_mode=RuntimeMode.WORKER)
    await _invalidate_and_rewarm_channel("ch-1", req)
    assert reg.invalidated == ["ch-1"]
    assert reg.rewarmed == []  # gated out by _owns_inbound


@pytest.mark.asyncio
async def test_update_hook_noop_without_registry():
    """No channel_registry on app.state -> the update hook is a no-op."""
    from primer.api.routers.channels import _invalidate_and_rewarm_channel

    req = type("_R", (), {})()
    req.app = type("_App", (), {"state": type("_S", (), {})()})()
    await _invalidate_and_rewarm_channel("ch-1", req)  # must not raise


@pytest.mark.asyncio
async def test_channel_update_enabling_chats_warms_adapter_end_to_end(
    tmp_path: Path,
):
    """End-to-end GAP-10: a chats-disabled channel is dark; after the operator
    flips chats.enabled true and the UPDATE hook runs, the inbound adapter is
    live WITHOUT a restart (no warm_chat_channels / boot involved)."""
    from primer.api.routers.channels import _invalidate_and_rewarm_channel

    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    try:
        _register_recording_factory()
        await _seed_with_chats(p, enabled=False)
        reg = _build_registry(p)
        req = _StubRequest(reg, runtime_mode=RuntimeMode.API)
        try:
            # Before: chats disabled, nothing warm.
            assert reg.peek_adapter("ch-1") is None

            # Operator enables chats (persisted), then the UPDATE hook fires.
            c_storage = p.get_storage(Channel)
            row = await c_storage.get("ch-1")
            await c_storage.update(row.model_copy(update={
                "config": SlackChannelConfig(
                    chats=ChatConfig(enabled=True, default_agent="agent-x"),
                ),
            }))
            await _invalidate_and_rewarm_channel("ch-1", req)

            # After: the inbound gateway is online live.
            assert reg.peek_adapter("ch-1") is not None
        finally:
            await reg.aclose()
    finally:
        await p.aclose()
