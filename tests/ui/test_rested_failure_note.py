"""A session that RESTS after a transport failure says why and what to do (console review C-024, the console part; review of #664).

An interactive session whose turn failed with a transport code (``primer.session.dispatch._RESTING_FAILURE_CODES``) is not ENDED: it rests (``status`` waiting, ``ended_reason`` null) and every
session read serves ``last_turn_error {code, at}``, cleared when the next turn starts. The console drew the error card and nothing else: ``NV_endedLine`` runs for an ended session only, so the
advice of ``NV_failureWords`` (what to do about THAT failure) was shown nowhere for a session that is still alive, and the header chip and the phone list still said Ready/Waiting.

What this file pins, by running the real functions and components in V8 (``tests/ui/_mini_react.py``), not by reading the source:

* the note: ``NV_restedNoteLine`` (the gate) and ``NV_RestedNote`` (what is drawn), for every code the server rests on, with the advice equal to ``NV_failureWords(code).next``;
* the chip: ``NV_sessionStateChipView`` says "Last turn failed" for such a session (header and phone list), and only for one.

A few narrow source pins remain for the wiring a V8 slice cannot see (the big doc component renders the note, the follow effect depends on it, the tap's error frame refetches the row).
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

import pytest

from primer.session.dispatch import _RESTING_FAILURE_CODES
from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")
MOBILE = (ROOT / "ui" / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")
STYLES = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

_NAMES = (
    "NV_failureWords", "NV_failureCodeWords", "NV_endedLine", "NV_restedFailureLine", "NV_newestTerminalRow", "NV_restedNoteLine", "NV_RestedNote",
    "NV_sessionStateChipView", "NV_SessionStateChip",
)


@functools.lru_cache(maxsize=1)
def _compiled() -> str:
    from primer.api._jsx_bundle import JSXBundler

    parts = []
    for name in _NAMES:
        start = DOC.find("function " + name + "(")
        if start >= 0:
            parts.append(DOC[start:DOC.index("\n}\n", start) + len("\n}\n")])
    bundler = JSXBundler(ui_dir=ROOT / "ui", babel_source=(ROOT / "ui" / "vendor" / "babel.min.js").read_text())
    try:
        return bundler._transform("\n".join(parts), "snippet.jsx")
    finally:
        bundler._ctx.close()


@pytest.fixture
def v8():
    made: list = []

    def build():
        ctx = mini_react_context(_compiled())
        made.append(ctx)
        return ctx

    try:
        yield build
    finally:
        for c in made:
            c.close()


def _call(ctx, expression: str):
    return json.loads(ctx.eval("JSON.stringify(" + expression + ")"))


def _resting(code: str | None, **extra) -> dict:
    return {"status": "waiting", "ended_reason": None, "session_state": "parked", "last_turn_error": None if code is None else {"code": code, "at": "2026-10-09T10:00:00Z"}, **extra}


ERROR_ROW = {"kind": "error", "seq": 9}


def _note(ctx, session, shown=False, newest=ERROR_ROW):
    return _call(ctx, f"NV_restedNoteLine({json.dumps(session)}, {json.dumps(shown)}, {json.dumps(newest)})")


# ---------------------------------------------------------------------------
# the words: every code the server rests on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(_RESTING_FAILURE_CODES))
def test_the_note_gives_the_failures_own_advice_for_every_code_the_server_rests_on(v8, code: str) -> None:
    ctx = v8()
    words = _call(ctx, f"NV_failureWords({json.dumps(code)})")
    assert words, f"{code}: the table has no words for a code the server rests on"
    got = _note(ctx, _resting(code))
    assert got["next"] == words["next"], (code, got, words)
    assert got["why"].startswith("This session is waiting: its last turn failed"), got
    assert code not in got["why"] + got["next"], "the raw code is not user language"


def test_the_note_names_the_state_and_does_not_repeat_the_error_card(v8) -> None:
    """The card right above it already says what happened; the note says what STATE the session is in, and what to do."""
    got = _note(v8(), _resting("server_error"))
    assert got["why"] == "This session is waiting: its last turn failed."
    assert "provider had a server error" not in got["why"]


def test_a_stream_that_died_with_no_code_gets_words_that_match_its_card_and_the_transport_advice(v8) -> None:
    ctx = v8()
    words = _call(ctx, 'NV_failureWords("llm_stream_error")')
    assert words and words["what"] == "the model stopped answering part-way through"
    assert "try again" in words["next"].lower()


def test_an_unknown_code_has_one_form_across_the_note_and_the_end_note(v8) -> None:
    ctx = v8()
    note = _note(ctx, _resting("weird_code_42"))
    ended = _call(ctx, 'NV_endedLine({"status": "ended", "ended_reason": "failed", "ended_detail": "weird_code_42"})')
    assert "the failure code is weird_code_42" in note["why"], note
    assert "the failure code is weird_code_42" in ended["why"], ended
    assert "trace" in note["next"].lower() and "send a message" in note["next"].lower()


def test_turn_failed_names_nothing_a_person_can_act_on(v8) -> None:
    got = _note(v8(), _resting("turn_failed"))
    assert got["why"] == "This session is waiting: its last turn failed." and got["next"]


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_no_note_without_a_failure_on_the_row(v8) -> None:
    ctx = v8()
    assert _note(ctx, _resting(None)) is None
    assert _note(ctx, {"status": "waiting"}) is None
    assert _note(ctx, None) is None


def test_no_note_for_an_ended_session(v8) -> None:
    """The control: its end divider and end note say it (the row of a failed ended session carries ``last_turn_error`` too)."""
    assert _note(v8(), {**_resting("server_error"), "status": "ended", "ended_reason": "failed"}) is None


def test_no_note_while_a_turn_is_shown_running(v8) -> None:
    """The server clears ``last_turn_error`` when the next turn starts; until the row says so a turn in flight hides the note."""
    assert _note(v8(), _resting("server_error"), shown=True) is None


def test_the_note_draws_only_when_the_newest_visible_terminal_row_is_the_error(v8) -> None:
    """After a Retry (a new user message), a rewind that kept the stamp, or a successful answer, the stamp on the row is not about what is on screen."""
    ctx = v8()
    for kind in ("user_message", "assistant_message", "done", "cancelled"):
        assert _note(ctx, _resting("server_error"), newest={"kind": kind, "seq": 9}) is None, kind
    assert _note(ctx, _resting("server_error"), newest=None) is None
    assert _note(ctx, _resting("server_error"), newest=ERROR_ROW) is not None


def test_the_newest_terminal_row_skips_what_is_not_terminal(v8) -> None:
    ctx = v8()
    rows = [{"kind": "user_message", "seq": 1}, {"kind": "error", "seq": 2}, {"kind": "divider", "seq": 3}, {"kind": "retry_notice", "seq": 4}, {"kind": "tool_call", "seq": 5}]
    assert _call(ctx, f"NV_newestTerminalRow({json.dumps(rows)})")["seq"] == 2
    assert _call(ctx, f"NV_newestTerminalRow({json.dumps(rows + [{'kind': 'user_message', 'seq': 6}])})")["seq"] == 6
    assert _call(ctx, "NV_newestTerminalRow([])") is None
    assert _call(ctx, "NV_newestTerminalRow(null)") is None


# ---------------------------------------------------------------------------
# what is drawn
# ---------------------------------------------------------------------------


def _mount(ctx, **props) -> list[str]:
    ctx.eval(f"MR.mount(NV_RestedNote, {json.dumps(props)});")
    return _call(ctx, "MR.texts()")


def test_the_component_draws_the_state_line_and_the_advice(v8) -> None:
    ctx = v8()
    texts = _mount(ctx, session=_resting("server_error"), shown=False, newest=ERROR_ROW, live=False)
    words = _call(ctx, 'NV_failureWords("server_error")')
    assert texts == ["This session is waiting: its last turn failed.", words["next"]], texts
    assert ctx.eval('MR.find("nv-rested-note") !== null') is True


@pytest.mark.parametrize("props", [
    {"shown": True, "newest": ERROR_ROW},
    {"shown": False, "newest": {"kind": "user_message", "seq": 9}},
    {"shown": False, "newest": None},
])
def test_the_component_draws_nothing_when_the_gate_says_no(v8, props: dict) -> None:
    ctx = v8()
    assert _mount(ctx, session=_resting("server_error"), live=False, **props) == []
    assert ctx.eval('MR.find("nv-rested-note") === null') is True


def test_the_component_announces_itself_only_when_the_failure_arrived_live(v8) -> None:
    """Gated like the card above it (``role=alert`` only for a failure that arrived live): a note drawn for a session opened on a failure is not announced."""
    ctx = v8()
    _mount(ctx, session=_resting("server_error"), shown=False, newest=ERROR_ROW, live=True)
    assert ctx.eval('MR.find("nv-rested-note").props.role') == "status"
    _mount(ctx, session=_resting("server_error"), shown=False, newest=ERROR_ROW, live=False)
    assert ctx.eval('MR.find("nv-rested-note").props.role === undefined') is True


# ---------------------------------------------------------------------------
# the chip (header and phone list)
# ---------------------------------------------------------------------------


def _chip(ctx, session):
    return _call(ctx, f"NV_sessionStateChipView({json.dumps(session)})")


@pytest.mark.parametrize("state", ["parked", "waiting"])
def test_a_session_resting_after_a_failure_says_so_in_its_chip(v8, state: str) -> None:
    """``parked`` once it has completed a turn, ``waiting`` for a failure of its very first turn: the chip said Ready/Waiting for both."""
    view = _chip(v8(), _resting("server_error", session_state=state))
    assert view["label"] == "Last turn failed" and view["failed"] is True, view
    assert view["state"] == state, "data-state stays as served"


@pytest.mark.parametrize("override", [
    {"last_turn_error": None},
    {"status": "ended", "ended_reason": "failed"},
    {"session_state": "running"},
    {"parked_status": "parked"},
    {"status": "paused"},
])
def test_the_chip_says_failed_only_for_a_session_that_rests_with_a_failure(v8, override: dict) -> None:
    view = _chip(v8(), {**_resting("server_error"), **override})
    assert view.get("failed") in (False, None) and view["label"] != "Last turn failed", (override, view)


def test_the_chip_keeps_its_other_words(v8) -> None:
    ctx = v8()
    assert _chip(ctx, {"session_state": "parked", "status": "waiting"})["label"] == "Ready"
    assert _chip(ctx, {"session_state": "parked", "status": "paused"})["label"] == "Paused"
    assert _chip(ctx, {"session_state": "parked", "parked_status": "parked"})["label"] == "Parked"
    assert _chip(ctx, {"session_state": "running"})["label"] == "Running"


def test_the_chip_component_carries_data_failed_for_the_style(v8) -> None:
    ctx = v8()
    ctx.eval(f"MR.mount(NV_SessionStateChip, {json.dumps({'session': _resting('server_error')})});")
    assert ctx.eval('MR.find("nv-session-state-chip").props["data-failed"]') == "true"
    ctx.eval(f"MR.mount(NV_SessionStateChip, {json.dumps({'session': _resting(None)})});")
    assert ctx.eval('MR.find("nv-session-state-chip").props["data-failed"] === undefined') is True


# ---------------------------------------------------------------------------
# the wiring a V8 slice cannot see
# ---------------------------------------------------------------------------


def test_the_doc_hands_the_note_its_gate_inputs_and_places_it_after_the_end_note() -> None:
    spot = DOC.index("<NV_RestedNote")
    tag = DOC[spot:DOC.index("/>", spot)]
    assert "shown={!!shown}" in tag and "newest={newestTerminal}" in tag and "session={session}" in tag, tag
    assert DOC.index('data-testid="nv-ended-note"') < spot < DOC.index("nv-jump-wrap")


def test_the_follow_effect_reacts_to_the_note_and_the_end_note_appearing() -> None:
    """A note that appears under the last row grows the pane: a follower must scroll to it, as it does for the end note."""
    effect = re.search(r"React\.useEffect\(function \(\) \{\s*if \(follow\) jumpLatest\(\);\s*\}, \[([^\]]*)\]\);", DOC)
    assert effect, "the follow effect moved"
    deps = effect.group(1)
    assert "!!restedLine" in deps and "!!endedLine" in deps, deps


def test_the_taps_error_frame_refetches_the_row() -> None:
    """A failed turn's error frame is the moment ``last_turn_error`` lands on the row; waiting for the next poll left the old state on screen."""
    listener = DOC[DOC.index("window.useWorkspaceTapListener(con.wid"):]
    listener = listener[:listener.index("});")]
    assert 'ev["class"] === "error"' in listener


def test_the_phone_list_shows_the_same_words_and_marks_the_failure() -> None:
    assert "window.NV_sessionStateChipView(s)" in MOBILE
    assert 'data-failed={stateView.failed ? "true" : undefined}' in MOBILE


def test_the_chip_has_a_style_for_the_failure() -> None:
    assert '.nv-session-state-chip[data-failed="true"]' in STYLES
