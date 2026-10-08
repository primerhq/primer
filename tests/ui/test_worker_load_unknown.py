"""A worker that has not reported its load is UNKNOWN, not idle (follow-up to #484, lead 2026-10-08).

``GET /v1/workers`` rows and ``/v1/health`` now carry ``in_flight: null`` for a worker from before load reporting (or one that has not sent
its first heartbeat with a load). The console read ``typeof w.in_flight === "number" ? w.in_flight : 0`` and ``w.in_flight || 0``, so such a
worker drew an empty bar and "0 / 4 slots in use", and the fleet summed to a confident total over rows that never reported: an
overloaded fleet of old workers read as idle. The helpers below are evaluated in V8; the page is driven in a real browser by
``tests/ui_e2e/test_worker_load_unknown_journey.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKERS = (ROOT / "ui" / "components" / "workers.jsx").read_text(encoding="utf-8")
HEALTH = (ROOT / "ui" / "components" / "health.jsx").read_text(encoding="utf-8")


@pytest.fixture
def ctx():
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    for name in ("WK_loadText", "WK_fleetLoad"):
        start = WORKERS.index("function " + name + "(")
        c.eval(WORKERS[start:WORKERS.index("\n}\n", start) + len("\n}\n")])
    try:
        yield c
    finally:
        c.close()


def _call(ctx, expression: str):
    return json.loads(ctx.eval("JSON.stringify(" + expression + ")"))


def test_a_reported_load_reads_as_used_over_capacity(ctx) -> None:
    assert _call(ctx, "WK_loadText(2, 4)") == "2 / 4"
    assert _call(ctx, "WK_loadText(0, 4)") == "0 / 4", "zero is a reported load, not an unknown one"


def test_an_unreported_load_reads_as_unknown_never_as_zero(ctx) -> None:
    assert _call(ctx, "WK_loadText(null, 4)") == "? / 4"
    assert _call(ctx, "WK_loadText(undefined, 4)") == "? / 4"


def test_the_fleet_sums_only_what_was_reported_and_says_how_much_is_missing(ctx) -> None:
    got = _call(ctx, 'WK_fleetLoad([{status: "active", in_flight: 2, capacity: 4}, {status: "active", in_flight: null, capacity: 3}])')
    assert got == {"flight": 2, "cap": 7, "unknown": 1, "known": False}


def test_a_fleet_that_all_reported_is_known(ctx) -> None:
    got = _call(ctx, 'WK_fleetLoad([{status: "active", in_flight: 2, capacity: 4}, {status: "draining", in_flight: 1, capacity: 3}])')
    assert got == {"flight": 3, "cap": 7, "unknown": 0, "known": True}


def test_dead_workers_are_tombstones_and_count_for_nothing_even_when_unreported(ctx) -> None:
    got = _call(ctx, 'WK_fleetLoad([{status: "active", in_flight: 1, capacity: 2}, {status: "dead", in_flight: null, capacity: 9}])')
    assert got == {"flight": 1, "cap": 2, "unknown": 0, "known": True}


def test_an_empty_fleet_is_a_known_zero(ctx) -> None:
    assert _call(ctx, "WK_fleetLoad([])") == {"flight": 0, "cap": 0, "unknown": 0, "known": True}


def test_the_page_no_longer_turns_a_missing_load_into_zero() -> None:
    assert 'in_flight: typeof w.in_flight === "number" ? w.in_flight : 0' not in WORKERS
    assert "w.in_flight || 0" not in WORKERS
    assert "WK_fleetLoad(" in WORKERS and "WK_loadText(" in WORKERS


def test_the_capacity_bar_draws_an_unreported_load_as_unknown() -> None:
    bar = WORKERS[WORKERS.index("function CapacityBar"):WORKERS.index("function SummaryStat")]
    assert "inFlight == null" in bar and 'title="Load not reported"' in bar


def test_the_legacy_health_page_does_not_turn_an_unreported_load_into_zero() -> None:
    assert 'typeof wp.in_flight === "number" ? wp.in_flight : 0' not in HEALTH
    assert "inFlight == null" in HEALTH
