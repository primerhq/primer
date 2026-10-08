"""When the auth gate asks again, and what it cleans up (review of PR 509).

A failed ``GET /v1/auth/status`` is retried on its own after ``AUTH_RETRY_MS`` when asking again can change the answer: a dead connection, a
server error, and the two 4xx that mean "later" (408, 429). Any other 4xx (a 401, a 403, a 404...) is an answer the server meant; polling it
every five seconds for as long as the tab is open changes nothing and adds load, so the gate shows what it said and waits for "Try again".
The pending retry is a timer: it must be cleared when the gate leaves the tree and when a click starts the next attempt, or two would run.

The real ``AuthGate`` runs in V8 through ``tests/ui/_mini_react.py`` with a fake clock and a scripted ``apiFetch``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

AUTH = Path(__file__).resolve().parents[2] / "ui" / "components" / "auth.jsx"

_PRELUDE = """
window.location = { search: "", pathname: "/console/", hash: "", href: "http://x/console/" };
window.history = { replaceState: function () {} };
var document = { title: "", documentElement: { getAttribute: function () { return "light"; } } };
var __timers = []; var __next = 1;
globalThis.setTimeout = function (fn, ms) { var id = __next++; __timers.push({ id: id, fn: fn, ms: ms, cleared: false }); return id; };
globalThis.clearTimeout = function (id) { __timers.forEach(function (t) { if (t.id === id) t.cleared = true; }); };
var __fetches = 0; var __failure = null; var __status = { has_user: true, authenticated: true, setup_complete: true };
window.primerApi = { apiFetch: function () { __fetches++; return __failure ? Promise.reject(__failure) : Promise.resolve(__status); } };
"""


@pytest.fixture
def gate():
    code = transpile(AUTH)
    made = []

    def make(failure: dict | None):
        ctx = mini_react_context(code, _PRELUDE)
        made.append(ctx)
        ctx.eval("__failure = " + json.dumps(failure))
        ctx.eval("function Host(props) { return props.show ? React.createElement(AuthGate, null, 'APP') : null; }")
        ctx.eval("MR.mount(Host, { show: true })")
        ctx.eval("MR.rerender({ show: true })")
        return ctx

    yield make
    for ctx in made:
        ctx.close()


def _timers(ctx) -> list[dict]:
    return json.loads(ctx.eval("JSON.stringify(__timers)"))


def _pending(ctx) -> list[dict]:
    return [t for t in _timers(ctx) if not t["cleared"]]


@pytest.mark.parametrize("failure", [
    {"name": "ApiError", "status": 0},
    {"name": "TypeError", "message": "Failed to fetch"},
    {"name": "ApiError", "status": 500}, {"name": "ApiError", "status": 502}, {"name": "ApiError", "status": 503},
    {"name": "ApiError", "status": 408}, {"name": "ApiError", "status": 429},
])
def test_a_failure_asking_again_can_fix_is_retried_on_its_own(gate, failure) -> None:
    ctx = gate(failure)
    pending = _pending(ctx)
    assert len(pending) == 1 and pending[0]["ms"] == 5000, f"one retry, after AUTH_RETRY_MS: {pending}"
    before = ctx.eval("__fetches")
    ctx.eval("__timers[__timers.length - 1].fn()")
    ctx.eval("MR.rerender({ show: true })")
    assert ctx.eval("__fetches") > before, "the timer asks again"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 410, 422])
def test_an_answer_the_server_meant_is_not_polled_but_waits_for_a_click(gate, status: int) -> None:
    ctx = gate({"name": "ApiError", "status": status})
    assert _timers(ctx) == [], f"a {status} must not schedule an automatic retry"
    assert ctx.eval("!!MR.find('auth-unreachable')"), "the failure screen is shown"
    texts = " ".join(json.loads(ctx.eval("JSON.stringify(MR.texts())")))
    assert str(status) in texts and "retrying automatically" not in texts.lower(), "and it does not claim to retry"
    before = ctx.eval("__fetches")
    ctx.eval("MR.click('auth-retry')")
    ctx.eval("MR.rerender({ show: true })")
    assert ctx.eval("__fetches") > before, "Try again still asks"


def test_the_pending_retry_is_cleared_when_the_gate_leaves_the_tree(gate) -> None:
    ctx = gate({"name": "ApiError", "status": 503})
    assert len(_pending(ctx)) == 1
    ctx.eval("MR.rerender({ show: false })")
    assert _pending(ctx) == [], "a timer left running would set state on a gate that is gone and ask the server again"


def test_a_click_clears_the_pending_retry_so_two_do_not_run(gate) -> None:
    ctx = gate({"name": "ApiError", "status": 503})
    first = _pending(ctx)[0]["id"]
    ctx.eval("MR.click('auth-retry')")
    ctx.eval("MR.rerender({ show: true })")
    assert first not in [t["id"] for t in _pending(ctx)], "the click's own attempt replaces the old timer"
    assert len(_pending(ctx)) <= 1


def test_a_status_that_arrives_ends_the_retrying(gate) -> None:
    ctx = gate({"name": "ApiError", "status": 503})
    ctx.eval("__failure = null")
    ctx.eval("__timers[__timers.length - 1].fn()")
    ctx.eval("MR.rerender({ show: true })")
    ctx.eval("MR.rerender({ show: true })")
    assert _pending(ctx) == []
    assert "APP" in " ".join(json.loads(ctx.eval("JSON.stringify(MR.texts())")))
