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


def test_legacy_pathological_text_pattern_times_out_as_no_match(monkeypatch):
    # (a|aa)+$ passes the nested-repeat check and backtracks exponentially in
    # both engines (about 90 s here unbounded): the 10 ms bound (#812) answers
    # "no match" instead of stalling the inbound path.
    import primer.common.user_regex as user_regex

    bounds: list[float] = []
    real_timed = user_regex.timed

    def _recording(call, *, timeout, **kwargs):
        bounds.append(timeout)
        return real_timed(call, timeout=timeout, **kwargs)

    monkeypatch.setattr(user_regex, "timed", _recording)
    matcher = EventMatcher(
        event_type=NormalizedEventType.MESSAGE_POSTED,
        text_pattern=r"(a|aa)+$",
    )
    started = time.perf_counter()
    assert matches(matcher, _ev(text="a" * 60 + "!")) is False
    elapsed = time.perf_counter() - started
    assert bounds == [0.01], bounds
    assert 0.009 <= elapsed < 0.1, elapsed


# ---- G6-5: the pattern's size, its compile cost and its dialect -------------------------------------------------------


def test_text_pattern_is_capped_at_1000_characters():
    validate_text_pattern("a" * 1000)
    with pytest.raises(EventMatcherPatternError, match="1001 characters long; the limit is 1000"):
        validate_text_pattern("a" * 1001)


def test_text_pattern_refuses_counted_repeats_that_unroll_past_the_cap_before_compiling():
    # The regex engine unrolls counted repeats when it compiles; the check
    # prices the engine's own parse tree first. The blow-up shapes themselves
    # ((?:a{65535}){65535} and the review's bypasses) are driven through
    # validate_text_pattern in a child process by
    # tests/common/test_user_regex_entry_points.py; this one would compile in
    # milliseconds even if the check regressed.
    with pytest.raises(EventMatcherPatternError, match="repeats too much"):
        validate_text_pattern("(?:a{101}){100}")
    validate_text_pattern(r"^\d{4}-\d{2}-\d{2}$")
    validate_text_pattern("(?:ab{1,30}){1,30}")


@pytest.mark.parametrize("pattern", [r"(?:deploy){e<=3}", r"(?:deploy){i<=1,s<=1}", r"deploy(?#x)", r"de(?i)ploy"])
def test_text_pattern_refuses_fuzzy_matching_comments_and_mid_pattern_flags(pattern):
    with pytest.raises(EventMatcherPatternError):
        validate_text_pattern(pattern)


# ---- round 2, B3: the budget is checked before compiling, compiles are charged, verdicts are cached ---------------------


def test_a_spent_budget_answers_before_compiling(monkeypatch):
    import primer.model.event_matcher as event_matcher_module
    from primer.model.event_matcher import EventTextBudget, match_outcome

    compiled: list[str] = []
    monkeypatch.setattr(event_matcher_module, "_compile_text_pattern", lambda p: compiled.append(p))
    budget = EventTextBudget()
    budget.charge(budget.seconds)
    matcher = EventMatcher(event_type=NormalizedEventType.MESSAGE_POSTED, text_pattern="never-compiled-[0-9]+")
    assert match_outcome(matcher, _ev(), budget=budget) == "budget_exhausted"
    assert compiled == []


def test_compile_time_is_charged_to_the_event_budget(monkeypatch):
    import primer.model.event_matcher as event_matcher_module
    from primer.model.event_matcher import EventTextBudget, match_outcome

    real = event_matcher_module._compile_text_pattern

    def _slow_compile(pattern):
        time.sleep(0.03)
        return real(pattern)

    monkeypatch.setattr(event_matcher_module, "_compile_text_pattern", _slow_compile)
    budget = EventTextBudget()
    matcher = EventMatcher(event_type=NormalizedEventType.MESSAGE_POSTED, text_pattern="^charged-compile$")
    assert match_outcome(matcher, _ev(text="other"), budget=budget) == "no_match"
    assert budget.spent >= 0.03


def test_a_refused_stored_pattern_is_not_checked_again(monkeypatch):
    import primer.common.user_regex as user_regex

    checks: list[str] = []
    real = user_regex.check_user_pattern

    def _counting(pattern, *args, **kwargs):
        checks.append(pattern)
        return real(pattern, *args, **kwargs)

    monkeypatch.setattr(user_regex, "check_user_pattern", _counting)
    legacy = "(?:" + "|".join(f"r{i:06d}" for i in range(200)) + "|deploy)"  # stored before the length cap
    matcher = EventMatcher(event_type=NormalizedEventType.MESSAGE_POSTED, text_pattern=legacy)
    for _ in range(5):
        assert matches(matcher, _ev(text="deploy now")) is False
    assert checks == [legacy]


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
    # The save path compiles once (same engine, same dialect) but never fills the verdict cache
    # (#859 round 3, R2-8); the match path compiles once and caches it for every later event.
    assert compiled.count(pattern) == 2


def test_the_save_path_never_touches_the_verdict_cache():
    from primer.common.user_regex import stored_cache_size

    before = stored_cache_size()
    validate_text_pattern(r"^save-path-only-[0-9]+$")
    with pytest.raises(EventMatcherPatternError):
        validate_text_pattern(r"(?:a{101}){100}save-path-refusal")
    assert stored_cache_size() == before


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
