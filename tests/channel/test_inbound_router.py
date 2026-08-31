"""S6 P5: the inbound router is event-only.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 7. The chat-surface
``route`` path and the ``has_matching_rule`` pre-pass are gone: every
inbound message is a routed event.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.channel.correlation import CorrelationStore
from primer.channel.inbound_router import ChannelInboundRouter
from primer.model.channel import Channel, ChannelProviderType, TelegramChannelConfig
from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)
from tests.conftest import _FakeStorageProvider


def _channel() -> Channel:
    return Channel(
        id="ch-1", provider_id="cp-1",
        provider=ChannelProviderType.TELEGRAM, external_id="777",
        config=TelegramChannelConfig(),
    )


def _event() -> ChannelEvent:
    return ChannelEvent(
        provider=ChannelProviderType.TELEGRAM, provider_id="cp-1",
        event_id="ev-1", type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=datetime.now(UTC), channel_id="ch-1", surface="thread",
        thread_anchor="thr-1", sender=EventSender(external_id="u1"),
        text="hello",
    )


def test_chat_surface_entrypoints_are_gone():
    for attr in ("route", "open_thread_chat", "has_matching_rule"):
        assert not hasattr(ChannelInboundRouter, attr), (
            f"{attr} must be deleted with the chat surface"
        )


async def test_route_event_returns_the_outcome():
    sp = _FakeStorageProvider()
    router = ChannelInboundRouter(sp, CorrelationStore(sp))
    outcome = await router.route_event(event=_event(), channel=_channel())
    assert outcome.kind == "ignored"


async def test_route_event_accepts_media_parts():
    sp = _FakeStorageProvider()
    router = ChannelInboundRouter(sp, CorrelationStore(sp))
    outcome = await router.route_event(
        event=_event(), channel=_channel(), media_parts=[],
    )
    assert outcome.kind == "ignored"
