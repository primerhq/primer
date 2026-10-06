"""``reply_payloads_equal`` and ``classify_marker_payload`` (worker/yield_runtime.py).

``reply_payloads_equal`` is the one rule for "does this stored reply carry the same reply as that one": a cancel
marker compares by its reason (its ``cancelled_at`` is stamped when the marker is BUILT, so a resend differs there),
a timeout marker compares as the bare marker, anything else compares as it is, and the comparison is the JSON-typed
``json_equal`` that a storage ``where`` uses (``True`` is not ``1``; ``1`` equals ``1.0``). A marker is recognised by
its KEY being present, as the classifier recognises it.

``classify_marker_payload`` is the classifier's body over a raw stored dict, so the multi-event graph drain can
convert each accumulated entry exactly as the single-event path converts its one payload; the pins below hold the
two to the same answer for a fixed ``now``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primer.model.yield_ import YieldCancelled, YieldTimeout, Yielded
from primer.worker.yield_runtime import (
    ParkedState,
    classify_marker_payload,
    classify_resume_payload,
    make_cancelled_payload,
    make_timeout_payload,
    reply_payloads_equal,
)

_T1 = datetime(2026, 10, 6, 9, 0, 0, tzinfo=timezone.utc)
_T2 = _T1 + timedelta(seconds=7)


# ===========================================================================
# reply_payloads_equal: the d17 table
# ===========================================================================


_EQUAL = [
    pytest.param(
        make_cancelled_payload(reason="stop", cancelled_at=_T1),
        make_cancelled_payload(reason="stop", cancelled_at=_T2),
        id="cancelled-marker-built-twice-different-cancelled_at",
    ),
    pytest.param(make_timeout_payload(), make_timeout_payload(), id="timeout-marker-against-itself"),
    pytest.param({}, None, id="empty-against-none"),
    pytest.param({"result": 1}, {"result": 1.0}, id="int-against-equal-float"),
    pytest.param(
        {"__yield_timeout__": False}, make_timeout_payload(), id="timeout-marker-recognised-by-key-not-truth",
    ),
    pytest.param(
        {"__yield_cancelled__": False, "reason": "stop"},
        make_cancelled_payload(reason="stop", cancelled_at=_T1),
        id="cancel-marker-recognised-by-key-not-truth",
    ),
    pytest.param(
        {"__yield_timeout__": True, "noise": 1}, make_timeout_payload(), id="timeout-marker-ignores-other-keys",
    ),
    pytest.param({"decision": "approved", "reason": None}, {"decision": "approved", "reason": None}, id="same-reply"),
]

_NOT_EQUAL = [
    pytest.param(
        make_cancelled_payload(reason="stop", cancelled_at=_T1),
        make_cancelled_payload(reason="superseded", cancelled_at=_T1),
        id="cancelled-markers-different-reasons",
    ),
    pytest.param({"result": 1}, {"result": True}, id="int-against-bool"),
    pytest.param({"result": "1"}, {"result": 1}, id="string-against-int"),
    pytest.param({"result": 1}, make_timeout_payload(), id="result-against-timeout-marker"),
    pytest.param(
        {"result": 1}, make_cancelled_payload(reason=None, cancelled_at=_T1), id="result-against-cancel-marker",
    ),
    pytest.param(
        make_timeout_payload(), make_cancelled_payload(reason=None, cancelled_at=_T1), id="timeout-against-cancel",
    ),
    pytest.param({"response": "blue"}, {"response": "blue", "extra": 1}, id="a-reply-with-one-more-key"),
]


@pytest.mark.parametrize(("a", "b"), _EQUAL)
def test_reply_payloads_equal_says_equal(a, b):
    assert reply_payloads_equal(a, b) is True
    assert reply_payloads_equal(b, a) is True, "the comparison must be symmetric"


@pytest.mark.parametrize(("a", "b"), _NOT_EQUAL)
def test_reply_payloads_equal_says_not_equal(a, b):
    assert reply_payloads_equal(a, b) is False
    assert reply_payloads_equal(b, a) is False, "the comparison must be symmetric"


def test_reply_payloads_equal_does_not_mutate_its_arguments():
    a = make_cancelled_payload(reason="stop", cancelled_at=_T1)
    b = make_cancelled_payload(reason="stop", cancelled_at=_T2)
    before_a, before_b = dict(a), dict(b)

    reply_payloads_equal(a, b)

    assert (a, b) == (before_a, before_b)


# ===========================================================================
# classify_marker_payload: the d18 pins against classify_resume_payload
# ===========================================================================


_PARKED_AT = datetime(2026, 10, 6, 8, 0, 0, tzinfo=timezone.utc)
_NOW = _PARKED_AT + timedelta(seconds=754.5)


def _parked(resume_event_payload: dict) -> ParkedState:
    return ParkedState(
        yielded=Yielded(tool_name="ask_user", event_key="ask_user:s:tc-1", resume_metadata={"prompt": "?"}),
        llm_messages=[],
        turn_no=0,
        # deliberately NOT the park's parked_at: elapsed_seconds is measured from the parked_at the caller passes
        started_at=_PARKED_AT - timedelta(hours=3),
        tool_call_id="tc-1",
        resume_event_payload=resume_event_payload,
    )


@pytest.mark.parametrize(
    ("raw", "expected_payload"),
    [
        pytest.param(
            {"response": "blue", "__yield_extra__": 1, "cancelled_at": "x"}, {"response": "blue"}, id="plain",
        ),
        pytest.param(make_timeout_payload(), YieldTimeout(elapsed_seconds=754.5), id="timeout"),
        pytest.param(
            make_cancelled_payload(reason="stop", cancelled_at=_T1),
            YieldCancelled(reason="stop", cancelled_at=_T1, elapsed_seconds=754.5),
            id="cancelled",
        ),
        pytest.param(
            {"__yield_cancelled__": True, "reason": None},
            YieldCancelled(reason=None, cancelled_at=_NOW, elapsed_seconds=754.5),
            id="cancelled-without-cancelled_at",
        ),
    ],
)
def test_the_single_event_classifier_and_the_marker_classifier_agree(raw, expected_payload):
    parked = _parked(raw)

    single = classify_resume_payload(parked, parked_at=_PARKED_AT, now=_NOW)
    marker = classify_marker_payload(parked.resume_event_payload, parked_at=_PARKED_AT, now=_NOW)

    assert marker == single
    assert marker.payload == expected_payload
    assert marker.elapsed_seconds == 754.5


def test_classify_marker_payload_leaves_the_stored_dict_alone():
    raw = {"response": "blue", "__yield_extra__": 1}

    classify_marker_payload(raw, parked_at=_PARKED_AT, now=_NOW)

    assert raw == {"response": "blue", "__yield_extra__": 1}


def test_classify_resume_payload_still_refuses_a_park_with_no_payload():
    with pytest.raises(ValueError, match="before resume_event_payload"):
        classify_resume_payload(_parked(None), parked_at=_PARKED_AT, now=_NOW)  # type: ignore[arg-type]
