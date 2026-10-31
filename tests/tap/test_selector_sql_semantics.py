"""A tap selector evaluates with SQL semantics and is validated before a stream starts (Part 2 Task 76: W26, W57a).

A comparison the data cannot satisfy is simply false, never an exception: ``LIKE`` on a value that is not text, ordering against a missing value or
across incomparable types. An exception inside the match killed the SSE stream after its 200 (the console reconnects and dies again) and wedged the
MCP drain's cursor for good. A field name that cannot resolve is refused when the selector is decoded (400 / bad request), not at the first record.
``%`` and ``_`` cross newlines, as in SQL, and the pattern must match the WHOLE value.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.model.storage import FieldRef, Op, Predicate, Value
from primer.tap.event import TapEvent, TapEventClass
from primer.tap.selector import _TAP_FIELDS, TapSelector, TapSelectorError, check_selector, event_matches, match_events


def _ev(payload, cls=TapEventClass.TOOL_CALL, seq=1):
    return TapEvent(
        cursor="c", seq=seq, workspace_id="w", session_id="s", agent_id=None, graph_id=None, class_=cls, ts=datetime.now(UTC), payload=payload,
    )


def _cmp(field, op, value):
    return TapSelector(events=Predicate(left=FieldRef(name=field), op=op, right=Value(value=value)))


@pytest.mark.parametrize("op", [Op.LIKE, Op.ILIKE])
@pytest.mark.asyncio
async def test_like_on_a_missing_or_non_text_value_is_false_not_an_error(op):
    sel = _cmp("payload.text", op, "%hello%")
    check_selector(sel)
    assert await match_events(sel, [_ev({"name": "x"}), _ev({"text": 5}), _ev({"text": None}), _ev({"text": "say hello"})]) == [False, False, False, True]


@pytest.mark.parametrize("op", [Op.GT, Op.LT, Op.GE, Op.LE])
def test_ordering_against_a_missing_value_is_false(op):
    assert event_matches(_cmp("payload.n", op, 1), _ev({})) is False


def test_ordering_across_incomparable_types_is_false():
    assert event_matches(_cmp("payload.n", Op.GT, 1), _ev({"n": "text"})) is False
    assert event_matches(_cmp("payload.n", Op.LT, "a"), _ev({"n": 3})) is False


def test_ordering_between_comparable_values_is_unchanged():
    assert event_matches(_cmp("payload.n", Op.GT, 1), _ev({"n": 2})) is True
    assert event_matches(_cmp("payload.n", Op.LE, 1), _ev({"n": 2})) is False
    assert event_matches(_cmp("session_id", Op.GE, "a"), _ev({})) is True


def test_equality_and_membership_and_null_checks_are_unchanged():
    assert event_matches(_cmp("class", Op.EQ, "tool_call"), _ev({})) is True
    assert event_matches(_cmp("payload.k", Op.IN, ["a", "b"]), _ev({"k": "b"})) is True
    assert event_matches(_cmp("payload.k", Op.IS_NULL, None), _ev({})) is True


def test_an_unknown_field_is_refused_when_the_selector_is_checked():
    with pytest.raises(TapSelectorError, match="bogus"):
        check_selector(_cmp("bogus", Op.EQ, 1))
    nested = TapSelector(events=Predicate(
        left=Predicate(left=FieldRef(name="class"), op=Op.EQ, right=Value(value="tool_call")),
        op=Op.AND,
        right=Predicate(left=FieldRef(name="nope"), op=Op.EQ, right=Value(value=1)),
    ))
    with pytest.raises(TapSelectorError, match="nope"):
        check_selector(nested)


def test_every_valid_field_form_passes_the_check():
    for name in ("class", "class_", "payload.anything", "payload.a.b", *sorted(_TAP_FIELDS)):
        check_selector(_cmp(name, Op.EQ, "x"))


def test_a_like_pattern_that_is_not_text_is_refused_when_the_selector_is_checked():
    with pytest.raises(TapSelectorError):
        check_selector(_cmp("payload.text", Op.LIKE, 5))


def test_like_wildcards_cross_newlines_like_sql():
    assert event_matches(_cmp("payload.text", Op.LIKE, "%world%"), _ev({"text": "hello\nworld"})) is True
    assert event_matches(_cmp("payload.text", Op.LIKE, "a_c"), _ev({"text": "a\nc"})) is True


def test_like_must_match_the_whole_value_a_trailing_newline_is_not_ignored():
    sel = _cmp("payload.text", Op.LIKE, "abc")
    assert event_matches(sel, _ev({"text": "abc"})) is True
    assert event_matches(sel, _ev({"text": "abc\n"})) is False
