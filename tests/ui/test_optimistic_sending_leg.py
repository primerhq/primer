"""The optimistic "sending" leg is dropped when the session SETTLES, not only when it ENDS (follow-up of #664, review of the C-024 console part).

Sending a message sets an optimistic flag so the composer says "sending" and the poll keeps going until the tap shows life: it is cleared when a live turn shows, or, as a bounded fallback,
when the polled row confirms the session settled with no live turn. That fallback only fired for an ENDED session (``NV_sessionIsOver``). Since #623 a failed send RESTS the session
(``status`` waiting, ``turn_status`` idle, ``last_turn_error`` on the row), so if the tap's frames arrive before the POST's response the leg is never cleared: the composer stays on
"sending" and the failure note stays hidden until a reload. ``NV_optimisticLegOver`` is the rule, a pure function of the polled row.

Also (a nit of the same review): the end note of a session that ended after a raised, non-transport failure names the failure code the row stamped when its ``ended_detail`` is empty.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")


@pytest.fixture
def v8():
    from py_mini_racer import MiniRacer

    made: list = []

    def build(*names: str):
        ctx = MiniRacer()
        made.append(ctx)
        for name in names:
            start = DOC.index("function " + name)
            ctx.eval(DOC[start:DOC.index("\n}\n", start) + len("\n}\n")])
        return ctx

    try:
        yield build
    finally:
        for c in made:
            c.close()


def _over(ctx, session, optimistic=1_000, now=12_000):
    return json.loads(ctx.eval(f"JSON.stringify(NV_optimisticLegOver({json.dumps(session)}, {optimistic}, {now}))"))


ENDED = {"status": "ended", "ended_reason": "failed", "turn_status": "idle"}
RESTING = {"status": "waiting", "ended_reason": None, "turn_status": "idle", "session_state": "parked", "last_turn_error": {"code": "server_error", "at": "t"}}


def test_a_settled_ended_session_drops_the_leg_after_ten_seconds(v8) -> None:
    """The control: the case the fallback always covered."""
    assert _over(v8("NV_sessionIsOver", "NV_optimisticLegOver"), ENDED) is True


def test_a_settled_resting_session_drops_the_leg_after_ten_seconds(v8) -> None:
    assert _over(v8("NV_sessionIsOver", "NV_optimisticLegOver"), RESTING) is True


def test_a_session_that_rests_without_a_failure_drops_it_too(v8) -> None:
    """Any settled row: a message that never reached a turn leaves the session waiting with nothing on it."""
    assert _over(v8("NV_sessionIsOver", "NV_optimisticLegOver"), {"status": "waiting", "turn_status": "idle", "session_state": "parked"}) is True


@pytest.mark.parametrize("session", [ENDED, RESTING], ids=["ended", "resting"])
def test_nothing_is_dropped_before_the_ten_seconds_are_up(v8, session: dict) -> None:
    assert _over(v8("NV_sessionIsOver", "NV_optimisticLegOver"), session, optimistic=5_000, now=12_000) is False


@pytest.mark.parametrize("turn_status", ["running", "claimable"])
def test_a_turn_that_is_queued_or_running_keeps_the_leg(v8, turn_status: str) -> None:
    assert _over(v8("NV_sessionIsOver", "NV_optimisticLegOver"), {**RESTING, "turn_status": turn_status}) is False


def test_no_leg_and_no_row_are_nothing_to_drop(v8) -> None:
    ctx = v8("NV_sessionIsOver", "NV_optimisticLegOver")
    assert _over(ctx, RESTING, optimistic=0) is False
    assert _over(ctx, None) is False


def test_the_effect_asks_the_rule_on_every_poll() -> None:
    effect = DOC[DOC.index("React.useEffect(function () { if (live) setOptimistic(null); }"):]
    effect = effect[:effect.index("[detail.data]")]
    assert re.search(r"NV_optimisticLegOver\(session, optimistic, Date\.now\(\)\)", effect), effect


def test_the_end_note_names_the_stamped_code_when_the_row_has_no_detail(v8) -> None:
    ctx = v8("NV_failureWords", "NV_failureCodeWords", "NV_endedLine")
    row = {"status": "ended", "ended_reason": "failed", "ended_detail": None, "last_turn_error": {"code": "weird_code_42", "at": "t"}}
    got = json.loads(ctx.eval(f"JSON.stringify(NV_endedLine({json.dumps(row)}))"))
    assert "the failure code is weird_code_42" in got["why"], got
    bare = json.loads(ctx.eval(f"JSON.stringify(NV_endedLine({json.dumps({**row, 'last_turn_error': {'code': 'turn_failed', 'at': 't'}})}))"))
    assert bare["why"] == "The turn failed; the error is in the transcript above.", "turn_failed names nothing a person can act on"
    none = json.loads(ctx.eval(f"JSON.stringify(NV_endedLine({json.dumps({**row, 'last_turn_error': None})}))"))
    assert none["why"] == "The turn failed; the error is in the transcript above."
