"""Channel trigger source."""

from datetime import datetime, timezone

from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)
from primer.model.trigger import ChannelTriggerConfig, Trigger
from primer.trigger.sources import get_source
from primer.trigger.sources.channel import ChannelSource


def _trigger() -> Trigger:
    return Trigger(
        id="tr-c",
        slug="tr-c",
        name="ch",
        config=ChannelTriggerConfig(provider_id="channel-provider-1"),
        created_at=datetime.now(timezone.utc),
    )


def test_channel_source_not_claim_eligible():
    source = ChannelSource()
    assert source.eligible_for_claim is False
    assert source.kind == "channel"
    assert (
        source.compute_next_fire_at(_trigger(), now=datetime.now(timezone.utc))
        is None
    )
    assert get_source("channel") is not None


def test_build_fire_context_puts_event():
    source = ChannelSource()
    fired_at = datetime.now(timezone.utc)
    ev = ChannelEvent(
        provider="slack",
        provider_id="channel-provider-1",
        event_id="ev-1",
        type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=fired_at,
        surface="channel",
        sender=EventSender(external_id="U1"),
    )
    ctx = source.build_fire_context(
        _trigger(), fired_at=fired_at, scheduled_for=None, event=ev
    )
    assert ctx["event"]["type"] == "message.posted"
    assert ctx["trigger_id"] == "tr-c"
    assert ctx["trigger_slug"] == "tr-c"
    assert ctx["kind"] == "channel"
    assert ctx["fired_at"] == fired_at.isoformat()
