"""A session that RESTS after a transport failure says why and what to do (console review C-024, the console part).

An interactive session whose turn failed with a transport code (``server_error``, ``rate_limit``, ``network_error``, ``connect_timeout``, ``stream_timeout``, ``generation_timeout``,
or a stream that died with no code) is no longer ENDED: it rests (``status=waiting``, ``ended_reason`` null) and every session read serves ``last_turn_error {code, at}``, cleared when the
next turn starts. The console drew the error card and nothing else: ``NV_endedLine`` runs for ``status=ended`` only, so the advice of ``NV_failureWords`` (what to do about THAT failure) was
shown nowhere for a session that is still alive. ``NV_restedFailureLine`` says it, under the transcript where the end note of an ended session sits.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")


@pytest.fixture
def line():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    for name in ("NV_failureWords", "NV_restedFailureLine"):
        start = DOC.index("function " + name)
        ctx.eval(DOC[start:DOC.index("\n}\n", start) + len("\n}\n")])
    try:
        yield lambda session: json.loads(ctx.eval("JSON.stringify(NV_restedFailureLine(" + json.dumps(session) + "))"))
    finally:
        ctx.close()


def _resting(code: str | None) -> dict:
    return {"status": "waiting", "ended_reason": None, "last_turn_error": None if code is None else {"code": code, "at": "2026-10-09T10:00:00Z"}}


@pytest.mark.parametrize(
    ("code", "what"),
    [
        ("server_error", "the model provider had a server error"),
        ("network_error", "the model provider could not be reached"),
        ("rate_limit", "the model provider is rate limiting requests"),
        ("stream_timeout", "the model stopped sending data in the middle of its answer"),
        ("generation_timeout", "the model took longer than the provider's total time limit"),
        ("connect_timeout", "the model provider did not accept the connection in time"),
    ],
)
def test_a_resting_session_says_what_failed_in_words_and_what_to_do_about_it(line, code: str, what: str) -> None:
    got = line(_resting(code))
    assert got["why"].startswith("The last turn failed: ") and what in got["why"], got
    assert got["next"] and code not in got["why"] + got["next"], "the advice is the failure's own, and the raw code is not user language"


def test_a_code_the_table_does_not_know_is_shown_rather_than_hidden(line) -> None:
    got = line(_resting("weird_code_42"))
    assert got["why"] == "The last turn failed (weird_code_42)." and "send a message" in got["next"].lower()


@pytest.mark.parametrize("code", ["llm_stream_error", "turn_failed"])
def test_the_generic_codes_dispatch_stamps_are_not_shown_as_if_they_were_words(line, code: str) -> None:
    """``llm_stream_error`` (a stream that died with no code) and ``turn_failed`` (a turn that raised something that is not a model error) name nothing a person can act on."""
    got = line(_resting(code))
    assert got["why"] == "The last turn failed." and got["next"], got


def test_a_session_with_no_failure_on_its_row_has_no_note(line) -> None:
    assert line(_resting(None)) is None
    assert line({"status": "waiting"}) is None
    assert line(None) is None


def test_an_ended_session_is_the_end_notes_not_this_ones(line) -> None:
    """The control: an ended session keeps ``NV_endedLine``'s divider and note; this one does not repeat it (the row of a failed ended session carries ``last_turn_error`` too)."""
    assert line({"status": "ended", "ended_reason": "failed", "last_turn_error": {"code": "server_error", "at": "t"}}) is None


def test_the_note_is_drawn_under_the_transcript_and_not_while_a_turn_runs() -> None:
    assert "NV_restedFailureLine(session)" in DOC
    assert re.search(r"var restedLine = shown \? null : NV_restedFailureLine\(session\);", DOC), "a turn in flight clears last_turn_error; until it does the note must not show"
    spot = DOC.index('data-testid="nv-rested-note"')
    assert "restedLine" in DOC[spot - 400:spot], "the note is gated on the line"
    assert DOC.index('data-testid="nv-ended-note"') < spot < DOC.index("nv-jump-wrap"), "under the transcript, next to the end note"
