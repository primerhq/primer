"""S6 P3: routing reports what it did, and answered gates stop shadowing
the thread mapping.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 5.
"""

from __future__ import annotations

from datetime import UTC, datetime

import primer.channel.event_dispatch as ed
from primer.channel.correlation import CorrelationStore
from primer.channel.event_dispatch import ChannelEventRouter, mapping_anchor
from primer.model.channel import Channel, ChannelProviderType, TelegramChannelConfig
from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.steer_delivery import DELIVERED_MISSING, SteerDelivery
from primer.trigger.subscribers import DispatchDeps
from tests.conftest import _FakeStorageProvider


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_key, payload=None):
        self.published.append((event_key, payload or {}))


def _channel() -> Channel:
    return Channel(
        id="ch-1", provider_id="cp-1",
        provider=ChannelProviderType.TELEGRAM, external_id="777",
        config=TelegramChannelConfig(),
    )


def _event(text: str = "hello") -> ChannelEvent:
    return ChannelEvent(
        provider=ChannelProviderType.TELEGRAM, provider_id="cp-1",
        event_id="ev-1", type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=datetime.now(UTC), channel_id="ch-1", surface="thread",
        thread_anchor="thr-1", sender=EventSender(external_id="u1"),
        text=text,
    )


def _dm_event(text: str = "hello") -> ChannelEvent:
    """A DM: no thread_anchor, because the platform has no thread here."""
    return ChannelEvent(
        provider=ChannelProviderType.TELEGRAM, provider_id="cp-1",
        event_id="ev-dm", type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=datetime.now(UTC), channel_id="ch-1", surface="dm",
        thread_anchor=None, sender=EventSender(external_id="u1"),
        text=text,
    )


def _router(sp, bus):
    return ChannelEventRouter(
        storage_provider=sp,
        correlation_store=CorrelationStore(sp),
        fire_deps=DispatchDeps(storage_provider=sp, claim_engine=None),
        event_bus=bus,
    )


def test_mapping_anchor_prefers_the_platform_thread():
    assert mapping_anchor(_event()) == "thr-1"


def test_mapping_anchor_treats_a_dm_as_its_own_thread():
    """S6 section 5: the DM anchor is a thread for mapping purposes."""
    assert mapping_anchor(_dm_event()) == "dm:u1"


def test_mapping_anchor_is_none_for_an_unthreaded_room_post():
    event = _dm_event()
    event.surface = "channel"
    assert mapping_anchor(event) is None


async def test_dm_gate_reply_resumes_on_the_dm_anchor():
    sp = _FakeStorageProvider()
    store = CorrelationStore(sp)
    await store.upsert_session(
        channel_id="ch-1", anchor="dm:u1", workspace_id="w1",
        session_id="s-dm", tool_call_id="tc-dm",
    )
    bus = _RecordingBus()
    outcome = await _router(sp, bus).route_event(
        event=_dm_event("yes"), channel=_channel(),
    )
    assert outcome.kind == "gate"
    assert outcome.session_id == "s-dm"
    assert bus.published == [("ask_user:s-dm:tc-dm", {"response": "yes"})]


async def test_gate_reply_resumes_then_clears_the_gate():
    sp = _FakeStorageProvider()
    store = CorrelationStore(sp)
    await store.upsert_session(
        channel_id="ch-1", anchor="thr-1", workspace_id="w1",
        session_id="s1", tool_call_id="tc-1",
    )
    bus = _RecordingBus()
    outcome = await _router(sp, bus).route_event(
        event=_event("yes"), channel=_channel(),
    )
    assert outcome.kind == "gate"
    assert outcome.session_id == "s1"
    assert bus.published == [("ask_user:s1:tc-1", {"response": "yes"})]
    record = await store.lookup("ch-1", "thr-1")
    assert record.tool_call_id is None, "an answered gate must not shadow the mapping"


async def test_uncorrelated_event_with_no_triggers_is_ignored():
    sp = _FakeStorageProvider()
    outcome = await _router(sp, _RecordingBus()).route_event(
        event=_event(), channel=_channel(),
    )
    assert outcome.kind == "ignored"


# ---------------------------------------------------------------------------
# US-012a: a gate reply whose session already ENDED (e.g. the yield timed
# out) must reopen the session like any other inbound message, mirroring
# deliver_steer's own wake_session-driven reopen - not publish onto a dead
# resume key nothing is listening for.
# ---------------------------------------------------------------------------


async def _seed_session(sp, *, session_id: str, status: SessionStatus) -> None:
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=session_id, workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=status, created_at=datetime.now(UTC),
    ))


async def test_ended_gate_target_reopens_via_deliver_steer(monkeypatch):
    sp = _FakeStorageProvider()
    await _seed_session(sp, session_id="s1", status=SessionStatus.ENDED)
    await CorrelationStore(sp).upsert_session(
        channel_id="ch-1", anchor="thr-1", workspace_id="w1",
        session_id="s1", tool_call_id="tc-1",
    )
    bus = _RecordingBus()
    captured: dict = {}

    async def _fake_deliver(**kw):
        captured.update(kw)
        return SteerDelivery(outcome="woken", session_id="s1")

    monkeypatch.setattr(ed, "deliver_steer", _fake_deliver)
    outcome = await _router(sp, bus).route_event(
        event=_event("yes"), channel=_channel(),
    )

    assert outcome.kind == "steer"
    assert outcome.session_id == "s1"
    assert captured["session_id"] == "s1"
    assert captured["text"] == "yes"
    # The dead resume key must never be published onto - nothing that
    # matters is listening for an ENDED session's ask_user gate.
    assert bus.published == []
    record = await CorrelationStore(sp).lookup("ch-1", "thr-1")
    assert record.tool_call_id is None, "the stale gate must still clear"


async def test_ended_gate_target_falls_through_when_unrestartable(monkeypatch):
    """A non-restartable ended_reason (workspace_lost/force_deleted) reports
    DELIVERED_MISSING - same fallback the plain steer branch already uses."""
    sp = _FakeStorageProvider()
    await _seed_session(sp, session_id="s1", status=SessionStatus.ENDED)
    await CorrelationStore(sp).upsert_session(
        channel_id="ch-1", anchor="thr-1", workspace_id="w1",
        session_id="s1", tool_call_id="tc-1",
    )

    async def _fake_deliver(**kw):
        return SteerDelivery(outcome=DELIVERED_MISSING)

    monkeypatch.setattr(ed, "deliver_steer", _fake_deliver)
    outcome = await _router(sp, _RecordingBus()).route_event(
        event=_event("yes"), channel=_channel(),
    )
    # No channel triggers seeded, so the fresh-thread fallback finds
    # nothing to fire.
    assert outcome.kind == "ignored"


async def test_non_ended_gate_target_keeps_the_direct_publish_path(monkeypatch):
    """A genuinely still-parked gate (status RUNNING/WAITING) must keep
    resuming via the direct bus publish - the ended-only reopen must not
    divert a live, answerable gate through deliver_steer instead."""
    sp = _FakeStorageProvider()
    await _seed_session(sp, session_id="s1", status=SessionStatus.WAITING)
    await CorrelationStore(sp).upsert_session(
        channel_id="ch-1", anchor="thr-1", workspace_id="w1",
        session_id="s1", tool_call_id="tc-1",
    )
    bus = _RecordingBus()

    async def _unexpected_deliver(**kw):
        raise AssertionError("deliver_steer must not be called for a live gate")

    monkeypatch.setattr(ed, "deliver_steer", _unexpected_deliver)
    outcome = await _router(sp, bus).route_event(
        event=_event("yes"), channel=_channel(),
    )

    assert outcome.kind == "gate"
    assert bus.published == [("ask_user:s1:tc-1", {"response": "yes"})]
