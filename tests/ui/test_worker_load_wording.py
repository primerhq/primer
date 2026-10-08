"""The words about a worker whose load is unknown (follow-up to #519).

The Workers page says how many workers did not report their load ("2 workers not reporting their load", not "2 workers not reporting its
load"), and the drain dialog says what it will wait for: a worker that has not reported its load has an UNKNOWN number of in-flight sessions,
and the dialog must not print "null in-flight sessions" or "0 in-flight sessions". Both texts are pure helpers evaluated here in V8.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

WORKERS = (Path(__file__).resolve().parents[2] / "ui" / "components" / "workers.jsx").read_text(encoding="utf-8")


@pytest.fixture
def ctx():
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    for name in ("WK_unreportedText", "WK_inFlightText"):
        start = WORKERS.index("function " + name + "(")
        c.eval(WORKERS[start:WORKERS.index("\n}\n", start) + len("\n}\n")])
    try:
        yield c
    finally:
        c.close()


def _call(ctx, expression: str):
    return json.loads(ctx.eval("JSON.stringify(" + expression + ")"))


@pytest.mark.parametrize(("n", "text"), [
    (1, "1 worker not reporting its load"),
    (2, "2 workers not reporting their load"),
    (7, "7 workers not reporting their load"),
])
def test_the_count_of_unreporting_workers_agrees_in_number(ctx, n: int, text: str) -> None:
    assert _call(ctx, f"WK_unreportedText({n})") == text


@pytest.mark.parametrize(("in_flight", "text"), [
    ("null", "Its in-flight sessions"),
    ("undefined", "Its in-flight sessions"),
    ("0", "0 in-flight sessions"),
    ("1", "1 in-flight session"),
    ("3", "3 in-flight sessions"),
])
def test_the_drain_dialog_names_an_unknown_number_of_sessions_without_inventing_one(ctx, in_flight: str, text: str) -> None:
    assert _call(ctx, f"WK_inFlightText({in_flight})") == text


def test_the_page_uses_the_helpers() -> None:
    assert "WK_unreportedText(load.unknown)" in WORKERS
    assert "WK_inFlightText(drainTarget.in_flight)" in WORKERS
    assert "not reporting its load" not in WORKERS.replace('"1 worker not reporting its load"', ""), "no second, hand-written copy of the sentence"
