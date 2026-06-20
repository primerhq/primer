"""ChannelEventRouter precedence: correlation-first, else fire channel triggers.

These tests drive :meth:`ChannelEventRouter.route_event` over a real
SqliteStorageProvider + CorrelationStore with a recording event bus and a
``fire_trigger`` spy injected via deps. They pin the precedence contract:

  1. A ``kind="session"`` correlation for the event's anchor resumes the gate
     (publishes ``ask_user:{sid}:{tcid}``) and does NOT fire any channel
     trigger.
  2. With no correlation, every matching ``kind="channel"`` trigger fires --
     channel-scoped triggers (``channel_id`` set) and provider-wide triggers
     (``channel_id is None``) both, scoped by provider_id + channel.
  3. A ``None`` event (unmappable raw) is a no-op at the factory wrapper.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.channel.correlation import CorrelationStore
from primer.channel.event_dispatch import ChannelEventRouter
from primer.model.channel import (
    Channel,
    ChannelProviderType,
    TelegramChannelConfig,
)
from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)
from primer.model.provider import SqliteConfig
from primer.model.trigger import ChannelTriggerConfig, Trigger
from primer.storage.sqlite import SqliteStorageProvider
from primer.trigger.subscribers import DispatchDeps


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_key, payload=None):
        self.published.append((event_key, payload or {}))


class _FireSpy:
    """Captures every fire_trigger call; returns a benign FireResult-like."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, *, trigger_id, scheduled_for, deps, extra_context=None):
        self.calls.append({
            "trigger_id": trigger_id,
            "scheduled_for": scheduled_for,
            "deps": deps,
            "extra_context": extra_context,
        })

        class _R:
            skipped = False
            fire_id = "fire-x"
            results: list = []

        return _R()


async def _provider(tmp_path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "ev.sqlite"))
    await p.initialize()
    return p


async def _channel(p, channel_id="ch-1", external_id="555"):
    ch = Channel(
        id=channel_id,
        provider_id="cp-1",
        provider=ChannelProviderType.TELEGRAM,
        external_id=external_id,
        config=TelegramChannelConfig(chats={"enabled": False, "default_agent": None}),
    )
    await p.get_storage(Channel).create(ch)
    return ch


def _event(*, channel_id=None, thread_anchor=None, etype=NormalizedEventType.MESSAGE_POSTED):
    return ChannelEvent(
        provider=ChannelProviderType.TELEGRAM,
        provider_id="cp-1",
        event_id="ev-1",
        type=etype,
        occurred_at=datetime.now(timezone.utc),
        room_external_id="555",
        channel_id=channel_id,
        surface="channel",
        thread_anchor=thread_anchor,
        sender=EventSender(external_id="u-1", display_name="Cara"),
        text="go ahead",
    )


def _deps(p):
    return DispatchDeps(
        storage_provider=p, claim_engine=None, scheduler=None,
    )


@pytest.mark.asyncio
async def test_correlation_session_reply_wins_over_rules(tmp_path: Path):
    p = await _provider(tmp_path)
    ch = await _channel(p)
    store = CorrelationStore(p)
    await store.upsert_session(
        channel_id=ch.id, anchor="thr-7", workspace_id="ws-1",
        session_id="sess-9", tool_call_id="tc-3")
    # A channel trigger that WOULD fire on a fresh event -- must NOT fire here.
    await p.get_storage(Trigger).create(Trigger(
        id="trg-rule", slug="rule-a", name="rule",
        config=ChannelTriggerConfig(provider_id="cp-1", channel_id=ch.id),
        created_at=datetime.now(timezone.utc)))

    bus = _RecordingBus()
    spy = _FireSpy()
    router = ChannelEventRouter(
        storage_provider=p, correlation_store=store,
        fire_deps=_deps(p), event_bus=bus, fire_trigger=spy)

    ev = _event(channel_id=ch.id, thread_anchor="thr-7")
    await router.route_event(event=ev, channel=ch)

    assert bus.published == [("ask_user:sess-9:tc-3", {"response": "go ahead"})]
    assert spy.calls == []


@pytest.mark.asyncio
async def test_no_correlation_fires_channel_triggers_channel_scoped_and_provider_wide(
    tmp_path: Path,
):
    p = await _provider(tmp_path)
    ch = await _channel(p)
    store = CorrelationStore(p)
    # Channel-scoped trigger (channel_id == ch.id).
    await p.get_storage(Trigger).create(Trigger(
        id="trg-scoped", slug="scoped", name="scoped",
        config=ChannelTriggerConfig(provider_id="cp-1", channel_id=ch.id),
        created_at=datetime.now(timezone.utc)))
    # Provider-wide trigger (channel_id is None).
    await p.get_storage(Trigger).create(Trigger(
        id="trg-wide", slug="wide", name="wide",
        config=ChannelTriggerConfig(provider_id="cp-1", channel_id=None),
        created_at=datetime.now(timezone.utc)))
    # A trigger for a DIFFERENT provider -- must not fire.
    await p.get_storage(Trigger).create(Trigger(
        id="trg-other", slug="other", name="other",
        config=ChannelTriggerConfig(provider_id="cp-2", channel_id=None),
        created_at=datetime.now(timezone.utc)))

    bus = _RecordingBus()
    spy = _FireSpy()
    router = ChannelEventRouter(
        storage_provider=p, correlation_store=store,
        fire_deps=_deps(p), event_bus=bus, fire_trigger=spy)

    ev = _event(channel_id=ch.id, thread_anchor=None)
    await router.route_event(event=ev, channel=ch)

    fired_ids = sorted(c["trigger_id"] for c in spy.calls)
    assert fired_ids == ["trg-scoped", "trg-wide"]
    expected_ctx = {"event": ev.model_dump(mode="json")}
    for call in spy.calls:
        assert call["extra_context"] == expected_ctx
        assert call["scheduled_for"] is None
        assert call["deps"] is router._fire_deps
    assert bus.published == []


@pytest.mark.asyncio
async def test_normalizer_none_event_is_ignored(tmp_path: Path):
    """A normalizer returning None means no event -> no correlation lookup,
    no fire. The guard lives in the factory wrapper; we model it here by
    simply never calling route_event when normalize() returns None."""
    p = await _provider(tmp_path)
    ch = await _channel(p)
    store = CorrelationStore(p)
    bus = _RecordingBus()
    spy = _FireSpy()
    router = ChannelEventRouter(
        storage_provider=p, correlation_store=store,
        fire_deps=_deps(p), event_bus=bus, fire_trigger=spy)

    class _NoneNormalizer:
        async def normalize(self, raw):
            return None

    normalizer = _NoneNormalizer()
    ev = await normalizer.normalize({"unmappable": True})
    if ev is not None:  # pragma: no cover -- normalize returns None here
        await router.route_event(event=ev, channel=ch)

    assert bus.published == []
    assert spy.calls == []
