"""UI-L1: the tap hub reopens an EventSource the browser closed.

A non-200 answer to the tap GET (a proxy's 502 or 504 while the API restarts, the server's own 503) makes the browser
close the EventSource for good (``readyState === 2``); only a dropped connection is retried natively. The hub
(``ui/foundation/use-workspace-tap.js``) used to flag ``connState = "error"`` and stay dead until every consumer
unmounted. It now reopens a closed one while it still has consumers, backing off 1 s, 2 s, 4 s ... up to 60 s, and the
delay resets once a connection opens.

MiniRacer over the real hub with a fake ``EventSource`` and a ``setTimeout`` the test fires by hand. Not run in a
browser.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from tests.ui._mini_react import ROOT

TAP_SRC = (ROOT / "ui" / "foundation" / "use-workspace-tap.js").read_text(encoding="utf-8")

PRELUDE = r"""
var window = globalThis;
var OPENED = [];
function EventSource(url) { this.url = url; this.readyState = 0; this.closed = false; OPENED.push(this); }
EventSource.prototype.close = function () { this.closed = true; this.readyState = 2; };
var TIMERS = [];
window.setTimeout = function (fn, ms) { TIMERS.push({ fn: fn, ms: ms, id: TIMERS.length + 1 }); return TIMERS.length; };
window.clearTimeout = function (id) { TIMERS = TIMERS.filter(function (t) { return t.id !== id; }); };
function fire() { var t = TIMERS.shift(); t.fn(); return t.ms; }
function last() { return OPENED[OPENED.length - 1]; }
function refused() { var es = last(); es.readyState = 2; es.onerror(); }
"""


@pytest.fixture
def ctx() -> Iterator[object]:
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    c.eval(PRELUDE)
    c.eval(TAP_SRC)
    try:
        yield c
    finally:
        c.close()


def js(c, expr: str):
    return json.loads(c.eval("JSON.stringify((function () { return " + expr + "; })())"))


def test_a_dropped_connection_is_left_to_the_browser(ctx) -> None:
    """readyState 0 on error: the browser is reconnecting by itself, so the hub only shows the drop."""
    ctx.eval("WTAP_retain('w1'); last().readyState = 0; last().onerror();")
    assert js(ctx, "OPENED.length") == 1 and js(ctx, "TIMERS.length") == 0
    assert js(ctx, "WTAP_hubConnState('w1')") == "error"


def test_a_closed_event_source_is_reopened_after_a_second(ctx) -> None:
    ctx.eval("WTAP_retain('w1'); refused();")
    assert js(ctx, "WTAP_hubConnState('w1')") == "error"
    assert js(ctx, "OPENED.length") == 1, "nothing reopens before the backoff"
    assert js(ctx, "fire()") == 1000
    assert js(ctx, "OPENED.length") == 2 and js(ctx, "OPENED[0].closed") is True
    assert js(ctx, "OPENED[1].url") == "/v1/workspaces/w1/tap"


def test_the_backoff_doubles_to_a_minute_and_resets_on_open(ctx) -> None:
    ctx.eval("WTAP_retain('w1');")
    delays = []
    for _ in range(8):
        ctx.eval("refused();")
        delays.append(js(ctx, "fire()"))
    assert delays == [1000, 2000, 4000, 8000, 16000, 32000, 60000, 60000]
    ctx.eval("last().onopen(); refused();")
    assert js(ctx, "fire()") == 1000


def test_no_reopen_once_the_last_consumer_left(ctx) -> None:
    ctx.eval("WTAP_retain('w1'); refused(); WTAP_release('w1');")
    ctx.eval("while (TIMERS.length) fire();")
    assert js(ctx, "OPENED.length") == 1
    assert js(ctx, "__wtapHubCount()") == 0
