"""A dead history backfill stays visible once events are on screen (UI-L9d).

``WorkspaceTap`` (``ui/components/workspace-tap.jsx``) shows the backfill's
failure - and its Retry - only while the event list is empty: the moment one
event was on screen (a live frame, or the backfill's own earlier page) a dead
backfill read as "Connected. Waiting for events…". The error is the backfill's
state, not the empty list's: it now renders whenever the backfill has failed,
events present or not.

MiniRacer on the real file with a deferred backfill answer the test rejects, and a
``useWorkspaceTap`` stub that hands the panel a live event.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
TAP = ROOT / "ui" / "components" / "workspace-tap.jsx"

PRELUDE = r"""
var window = globalThis;
globalThis.Btn = function (props) {
  return React.createElement("button", { "data-testid": props["data-testid"] || "tap-btn", onClick: props.onClick });
};
globalThis.Icon = function (p) { return React.createElement("span", null, p && p.name); };
globalThis.Banner = function (props) { return React.createElement("div", { "data-testid": "tap-banner" }, props.children); };
globalThis.relativeTime = function () { return ""; };
window.requestAnimationFrame = function () { return 0; };
window.cancelAnimationFrame = function () {};
var BF = null;         // the current backfill answer's reject
var FETCHES = 0;
window.__live = [];
window.useWorkspaceTap = function (wid) {
  return { events: window.__live, connState: "live", clear: function () {} };
};
window.primerApi = {
  useRouter: function () { return { navigate: function () {} }; },
  apiFetch: function (method, url) {
    if (url.indexOf("/events?limit=200") < 0) return Promise.resolve({});
    var d;
    var p = new Promise(function (_res, rej) { d = rej; });
    FETCHES++;
    BF = d;
    return p;
  },
};
"""

DRIVER = r"""
var REFUSAL = { status: 500, title: "Internal Server Error", detail: "the events read failed" };
function __failBackfill() { BF(REFUSAL); }
function __text() { return MR.texts().join(" "); }
"""


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


@pytest.fixture
def ctx() -> Iterator[object]:
    c = mini_react_context(transpile(TAP), PRELUDE)
    c.eval(DRIVER)
    c.eval("MR.mount(WorkspaceTap, { wid: 'w1', sessionId: null, fillHeight: false });")
    try:
        yield c
    finally:
        c.close()


def test_a_dead_backfill_with_no_events_shows_the_error_and_retry(ctx) -> None:
    # Characterisation: with the list empty, a failed backfill shows its error
    # and a Retry that refetches.
    ctx.eval("__failBackfill();")
    ctx.eval("MR.rerender();")
    assert _js(ctx, "MR.find('tap-history-error')") is not None
    assert "the events read failed" in _js(ctx, "__text()")
    ctx.eval("MR.click('tap-history-retry');")
    assert _js(ctx, "FETCHES") == 2, "the Retry does not refetch the backfill"


def test_a_dead_backfill_stays_visible_with_events_on_screen(ctx) -> None:
    # Red (UI-L9d): one live event is on screen; the dead backfill must not
    # disappear into "Connected. Waiting for events…".
    ctx.eval("window.__live = [{ session_id: 's1', seq: 1, class: 'done', payload: { stop_reason: 'done' }, ts: '2026-11-01T00:00:00Z' }];")
    ctx.eval("MR.rerender();")
    assert _js(ctx, "MR.findAll('tap-event-row').length") == 1, "the live event is not on screen"
    ctx.eval("__failBackfill();")
    ctx.eval("MR.rerender();")
    assert _js(ctx, "MR.find('tap-history-error')") is not None, (
        "the backfill's error is hidden once an event exists"
    )
    assert "the events read failed" in _js(ctx, "__text()")
