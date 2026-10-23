from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)
from primer.model.channel import ChannelProviderType
from primer.model.event_matcher import (
    EventMatcher,
    EventMatcherPatternError,
    matches,
    validate_text_pattern,
)


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


def test_text_pattern_rejects_nested_unbounded_repetition():
    with pytest.raises(EventMatcherPatternError, match="nested unbounded repetition"):
        validate_text_pattern(r"(a+)+$")


def test_text_pattern_rejects_invalid_regex():
    with pytest.raises(EventMatcherPatternError, match="valid regular expression"):
        validate_text_pattern(r"(unclosed")


def test_text_pattern_allows_bounded_nested_repetition():
    validate_text_pattern(r"^(?:https?://)?example\.com$")
    matcher = EventMatcher(
        event_type=NormalizedEventType.MESSAGE_POSTED,
        text_pattern=r"^(?:https?://)?example\.com$",
    )
    assert matches(matcher, _ev(text="https://example.com")) is True


def test_legacy_pathological_text_pattern_times_out_as_no_match():
    # (a|aa)+$ passes the nested-repeat check and backtracks exponentially in
    # both engines (about 90 s here unbounded): the 10 ms bound answers "no
    # match" instead of stalling the inbound path.
    matcher = EventMatcher(
        event_type=NormalizedEventType.MESSAGE_POSTED,
        text_pattern=r"(a|aa)+$",
    )
    started = time.perf_counter()
    assert matches(matcher, _ev(text="a" * 60 + "!")) is False
    assert time.perf_counter() - started < 0.5


# ---- G6-5: the pattern's size, its compile cost and its dialect -------------------------------------------------------


def test_text_pattern_is_capped_at_1000_characters():
    validate_text_pattern("a" * 1000)
    with pytest.raises(EventMatcherPatternError, match="1001 characters long; the limit is 1000"):
        validate_text_pattern("a" * 1001)


def test_text_pattern_refuses_counted_repeats_that_unroll_past_the_cap_before_compiling():
    # The regex engine unrolls counted repeats when it compiles: this
    # 19-character pattern never finishes compiling (and the stdlib check that
    # #812 used accepts it in microseconds).
    started = time.perf_counter()
    with pytest.raises(EventMatcherPatternError, match="repeats too much"):
        validate_text_pattern("(?:a{65535}){65535}")
    with pytest.raises(EventMatcherPatternError, match="repeats too much"):
        validate_text_pattern("(?:a{1000}){1000}")
    assert time.perf_counter() - started < 0.5
    validate_text_pattern(r"^\d{4}-\d{2}-\d{2}$")
    validate_text_pattern("(?:ab{1,30}){1,30}")


def test_save_time_validation_uses_the_matching_engine_dialect():
    # \p{Lu} is a regex-engine class the stdlib refuses: the save-time check
    # must accept what the matcher runs, in the same dialect.
    validate_text_pattern(r"^\p{Lu}eploy")
    matcher = EventMatcher(
        event_type=NormalizedEventType.MESSAGE_POSTED,
        text_pattern=r"^\p{Lu}eploy",
    )
    assert matches(matcher, _ev(text="Deploy now")) is True
    assert matches(matcher, _ev(text="deploy now")) is False


def test_save_time_validation_precompiles_the_pattern_the_matcher_uses(monkeypatch):
    import regex

    compiled: list[str] = []
    real = regex.compile

    def _counting(pattern, *args, **kwargs):
        compiled.append(pattern)
        return real(pattern, *args, **kwargs)

    monkeypatch.setattr(regex, "compile", _counting)
    pattern = r"^precompiled-[0-9a-f]{8} once$"
    validate_text_pattern(pattern)
    matcher = EventMatcher(event_type=NormalizedEventType.MESSAGE_POSTED, text_pattern=pattern)
    assert matches(matcher, _ev(text="precompiled-0123abcd once")) is True
    assert matches(matcher, _ev(text="nope")) is False
    assert compiled.count(pattern) == 1


def test_a_legacy_text_pattern_over_the_cap_never_matches():
    # A row stored before the cap: the matcher refuses to compile it rather
    # than paying its compile cost on every inbound event.
    matcher = EventMatcher(
        event_type=NormalizedEventType.MESSAGE_POSTED,
        text_pattern="(?:" + "|".join(f"w{i:06d}" for i in range(200)) + "|deploy)",
    )
    assert len(matcher.text_pattern) > 1000
    assert matches(matcher, _ev(text="deploy now")) is False


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
