from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from primer.model.channel import ChannelProviderType
from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)


def test_normalized_event_type_values():
    assert NormalizedEventType.MESSAGE_POSTED.value == "message.posted"
    assert NormalizedEventType.COMMAND_INVOKED.value == "command.invoked"
    assert NormalizedEventType.COMPONENT_ACTED.value == "component.acted"
    assert NormalizedEventType.REACTION_ADDED.value == "reaction.added"
    assert NormalizedEventType.REACTION_REMOVED.value == "reaction.removed"
    assert NormalizedEventType.MESSAGE_EDITED.value == "message.edited"
    assert NormalizedEventType.MEMBER_JOINED.value == "member.joined"
    assert NormalizedEventType.BOT_INSTALLED.value == "bot.installed"
    assert NormalizedEventType.BOT_REMOVED.value == "bot.removed"
    assert NormalizedEventType.ROOM_CREATED.value == "room.created"


def test_channel_event_round_trips():
    ev = ChannelEvent(
        provider=ChannelProviderType.SLACK,
        provider_id="channel-provider-1",
        event_id="ev-1",
        type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=datetime.now(timezone.utc),
        surface="channel",
        sender=EventSender(external_id="U123"),
        text="hi",
    )
    restored = ChannelEvent.model_validate(ev.model_dump())
    assert restored.type == NormalizedEventType.MESSAGE_POSTED
    assert restored.mentions_bot is False
    assert restored.media == []
    assert restored.raw == {}
    assert restored.sender.roles == []


def test_event_sender_defaults():
    sender = EventSender(external_id="U1")
    assert sender.display_name is None
    assert sender.roles == []
    assert sender.is_bot is False


def test_surface_rejects_unknown_value():
    with pytest.raises(ValidationError):
        ChannelEvent(
            provider=ChannelProviderType.SLACK,
            provider_id="channel-provider-1",
            event_id="ev-1",
            type=NormalizedEventType.MESSAGE_POSTED,
            occurred_at=datetime.now(timezone.utc),
            surface="elsewhere",
            sender=EventSender(external_id="U123"),
        )
