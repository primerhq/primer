from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)
from primer.model.channel import ChannelProviderType
from primer.model.event_matcher import EventMatcher, matches


def test_matcher_requires_event_type():
    with pytest.raises(ValidationError):
        EventMatcher()

    m = EventMatcher(event_type=NormalizedEventType.MESSAGE_POSTED)
    assert m.surface is None
    assert m.room_external_ids is None
    assert m.command_name is None
    assert m.mentions_bot is None
    assert m.sender_roles_any is None
    assert m.sender_ids_any is None
    assert m.text_pattern is None


def _ev(**over) -> ChannelEvent:
    base = dict(
        provider=ChannelProviderType.SLACK,
        provider_id="P1",
        event_id="E1",
        type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at="2026-06-20T00:00:00Z",
        surface="channel",
        room_external_id="C1",
        sender=EventSender(external_id="U1", roles=["admin"]),
        text="deploy now",
    )
    base.update(over)
    return ChannelEvent(**base)


def test_event_type_mismatch_fails():
    m = EventMatcher(event_type=NormalizedEventType.COMMAND_INVOKED)
    assert matches(m, _ev()) is False


def test_bare_event_type_matches():
    m = EventMatcher(event_type=NormalizedEventType.MESSAGE_POSTED)
    assert matches(m, _ev()) is True


def test_surface_subset():
    m = EventMatcher(
        event_type=NormalizedEventType.MESSAGE_POSTED,
        surface=["dm", "thread"],
    )
    assert matches(m, _ev(surface="channel")) is False
    assert matches(m, _ev(surface="dm")) is True


def test_room_allowlist():
    m1 = EventMatcher(
        event_type=NormalizedEventType.MESSAGE_POSTED,
        room_external_ids=["C2"],
    )
    assert matches(m1, _ev()) is False
    m2 = EventMatcher(
        event_type=NormalizedEventType.MESSAGE_POSTED,
        room_external_ids=["C1", "C2"],
    )
    assert matches(m2, _ev()) is True


def test_command_name():
    ev = _ev(type=NormalizedEventType.COMMAND_INVOKED, command={"name": "deploy"})
    m_deploy = EventMatcher(
        event_type=NormalizedEventType.COMMAND_INVOKED,
        command_name="deploy",
    )
    assert matches(m_deploy, ev) is True
    m_halt = EventMatcher(
        event_type=NormalizedEventType.COMMAND_INVOKED,
        command_name="halt",
    )
    assert matches(m_halt, ev) is False
    ev_no_cmd = _ev(type=NormalizedEventType.COMMAND_INVOKED, command=None)
    assert matches(m_deploy, ev_no_cmd) is False


def test_mentions_bot_true_and_false():
    m = EventMatcher(
        event_type=NormalizedEventType.MESSAGE_POSTED,
        mentions_bot=True,
    )
    assert matches(m, _ev(mentions_bot=False)) is False
    assert matches(m, _ev(mentions_bot=True)) is True


def test_text_pattern_regex():
    m_deploy = EventMatcher(
        event_type=NormalizedEventType.MESSAGE_POSTED,
        text_pattern=r"^deploy",
    )
    assert matches(m_deploy, _ev()) is True
    m_halt = EventMatcher(
        event_type=NormalizedEventType.MESSAGE_POSTED,
        text_pattern=r"halt",
    )
    assert matches(m_halt, _ev()) is False
    assert matches(m_deploy, _ev(text=None)) is False


def test_sender_roles_any():
    m_ops = EventMatcher(
        event_type=NormalizedEventType.MESSAGE_POSTED,
        sender_roles_any=["ops"],
    )
    assert matches(m_ops, _ev()) is False
    m_admin_ops = EventMatcher(
        event_type=NormalizedEventType.MESSAGE_POSTED,
        sender_roles_any=["admin", "ops"],
    )
    assert matches(m_admin_ops, _ev()) is True


def test_sender_ids_any():
    m_u9 = EventMatcher(
        event_type=NormalizedEventType.MESSAGE_POSTED,
        sender_ids_any=["U9"],
    )
    assert matches(m_u9, _ev()) is False
    m_u1 = EventMatcher(
        event_type=NormalizedEventType.MESSAGE_POSTED,
        sender_ids_any=["U1"],
    )
    assert matches(m_u1, _ev()) is True


def test_all_present_fields_anded():
    m = EventMatcher(
        event_type=NormalizedEventType.MESSAGE_POSTED,
        surface=["channel"],
        room_external_ids=["C1"],
        mentions_bot=True,
        sender_roles_any=["admin"],
        text_pattern=r"^deploy",
    )
    ev = _ev(mentions_bot=True)
    assert matches(m, ev) is True
    ev_miss = _ev(mentions_bot=False)
    assert matches(m, ev_miss) is False
