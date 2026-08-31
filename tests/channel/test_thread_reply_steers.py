"""S6 P3: a reply in a mapped thread appends to that session.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 5.
"""

from __future__ import annotations

from datetime import UTC, datetime

import primer.channel.event_dispatch as ed
from primer.channel.correlation import CorrelationStore
from primer.channel.event_dispatch import ChannelEventRouter
from primer.model.channel import Channel, ChannelProviderType, TelegramChannelConfig
from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)
from primer.session.steer_delivery import DELIVERED_MISSING, SteerDelivery
from primer.trigger.subscribers import DispatchDeps
from tests.conftest import _FakeStorageProvider


def _channel() -> Channel:
    return Channel(
        id="ch-1", provider_id="cp-1",
        provider=ChannelProviderType.TELEGRAM, external_id="777",
        config=TelegramChannelConfig(),
    )


def _event(text: str) -> ChannelEvent:
    return ChannelEvent(
        provider=ChannelProviderType.TELEGRAM, provider_id="cp-1",
        event_id="ev-2", type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=datetime.now(UTC), channel_id="ch-1", surface="thread",
        thread_anchor="thr-1", sender=EventSender(external_id="u1"),
        text=text,
    )


def _router(sp):
    return ChannelEventRouter(
        storage_provider=sp,
        correlation_store=CorrelationStore(sp),
        fire_deps=DispatchDeps(
            storage_provider=sp, claim_engine=object(), scheduler=object(),
            workspace_registry=object(),
        ),
        event_bus=None,
    )


async def test_reply_steers_the_mapped_session(monkeypatch):
    sp = _FakeStorageProvider()
    await CorrelationStore(sp).upsert_thread_session(
        channel_id="ch-1", anchor="thr-1", workspace_id="w1", session_id="s1",
    )
    captured: dict = {}

    async def _fake_deliver(**kw):
        captured.update(kw)
        return SteerDelivery(outcome="woken", session_id="s1")

    monkeypatch.setattr(ed, "deliver_steer", _fake_deliver)
    outcome = await _router(sp).route_event(
        event=_event("any update?"), channel=_channel(),
    )

    assert outcome.kind == "steer"
    assert outcome.session_id == "s1"
    assert captured["session_id"] == "s1"
    assert captured["text"] == "any update?"
    assert captured["parallelism"] == "queue"


async def test_mapping_to_a_deleted_session_falls_back_to_a_fresh_thread(
    monkeypatch,
):
    """S6 section 5: session deleted, thread lives on -> treat as new."""
    sp = _FakeStorageProvider()
    await CorrelationStore(sp).upsert_thread_session(
        channel_id="ch-1", anchor="thr-1", workspace_id="w1", session_id="gone",
    )

    async def _fake_deliver(**kw):
        return SteerDelivery(outcome=DELIVERED_MISSING)

    monkeypatch.setattr(ed, "deliver_steer", _fake_deliver)
    outcome = await _router(sp).route_event(
        event=_event("hello again"), channel=_channel(),
    )
    # No channel triggers seeded, so the fresh path finds nothing to fire.
    assert outcome.kind == "ignored"
