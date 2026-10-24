"""A paged list on the REAL ``ui/foundation/use-resource.js`` (#923, round 2): ``PR_fetchAllPages`` and ``PR_usePagedResource`` with the real cache, polls,
aborts and invalidations, not a stand-in for ``useResource``.

The fake ``apiFetch`` answers a page only when the test says so (``answerOne``), honours the abort signal the way the real one does (an aborted fetch rejects as
an ``ApiError`` network error), and pages the way the server does. Timers are recorded, not run (``TIMERS``), so the poll cadence is read, and a poll is
fired by hand. Derived from the reviewer's probe of round 2.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
API = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")
USE_RESOURCE = (ROOT / "ui" / "foundation" / "use-resource.js").read_text(encoding="utf-8")

_PRELUDE = r"""
var TIMERS = []; var TID = 0;
function setTimeout(fn, ms) { TID += 1; TIMERS.push({ id: TID, fn: fn, ms: ms }); return TID; }
function clearTimeout(id) { TIMERS = TIMERS.filter(function (t) { return t.id !== id; }); }
function AbortController() {
  var s = { aborted: false, _ls: [] };
  s.addEventListener = function (n, f) { s._ls.push(f); };
  this.signal = s;
  this.abort = function () { if (s.aborted) return; s.aborted = true; s._ls.forEach(function (f) { f(); }); };
}
var document = { hidden: false, addEventListener: function () {}, removeEventListener: function () {} };
"""

_SERVER = r"""
var TOTAL = 600; var URLS = []; var PENDING = []; var FAIL_AT = null;
window.primerApi.apiFetch = function (method, path, body, o) {
  URLS.push(path);
  var signal = o && o.signal;
  return new Promise(function (resolve, reject) {
    var offset = Number(/[?&]offset=(\d+)/.exec(path)[1]); var limit = Number(/[?&]limit=(\d+)/.exec(path)[1]);
    function aborted() { reject(new window.primerApi.ApiError({ type: "/errors/network-error", title: "Network error", status: 0 })); }
    if (signal && signal.aborted) { aborted(); return; }
    if (signal) signal.addEventListener("abort", aborted);
    PENDING.push(function () {
      if (FAIL_AT !== null && offset >= FAIL_AT) { reject(new window.primerApi.ApiError({ status: 503, title: "Service Unavailable", detail: "blip" })); return; }
      var n = Math.max(0, Math.min(limit, TOTAL - offset)), items = [];
      for (var i = 0; i < n; i++) items.push({ id: "a-" + (offset + i) });
      resolve({ kind: "offset", offset: offset, length: n, total: TOTAL, items: items });
    });
  });
};
function answerOne() { var f = PENDING.shift(); if (f) f(); return !!f; }
var LAST = null;
function Probe(props) {
  var res = PR_usePagedResource(props.k, function (signal) { return PR_fetchAllPages("/agents", { signal: signal }); }, { pollMs: props.pollMs || 15000 });
  LAST = res;
  return React.createElement("div", null,
    React.createElement("i", { "data-testid": "n" }, res.data ? String(res.data.items.length) : "none"),
    React.createElement(PR_PagedNote, { list: res.data, noun: "agents", testid: "note" }));
}
function Parent(props) { return props.show ? React.createElement(Probe, { k: props.k, pollMs: props.pollMs }) : null; }
function shown() { var el = MR.find("n"); return el ? el.children.join("") : null; }
function polls() { return TIMERS.map(function (t) { return t.ms; }); }
"""


@pytest.fixture
def ctx():
    c = mini_react_context(API + "\n" + USE_RESOURCE, _PRELUDE)
    c.eval(_SERVER)
    try:
        yield c
    finally:
        c.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def drain(ctx, n: int = 20) -> None:
    """Answer the pages one at a time, drawing after each (each eval ends with the microtask queue drained)."""
    for _ in range(n):
        if not ctx.eval("answerOne()"):
            break
        ctx.eval("MR.rerender();")
    ctx.eval("MR.rerender(); MR.rerender();")


def test_three_pages_are_read_and_the_poll_is_45_s_and_an_invalidation_is_immediate(ctx):
    ctx.eval("MR.mount(Parent, { show: true, k: 'a' });")
    drain(ctx)
    assert js(ctx, "shown()") == "600"
    assert js(ctx, "URLS") == ["/agents?limit=200&offset=0", "/agents?limit=200&offset=200", "/agents?limit=200&offset=400"]
    assert js(ctx, "polls()") == [45000]
    # a write's invalidation (useMutation: findKeys + refetchKey) is not slowed: the read starts at once
    ctx.eval("window.primerApi._resource.findKeys('a').forEach(window.primerApi._resource.refetchKey);")
    assert js(ctx, "URLS.length") == 4
    drain(ctx)
    assert js(ctx, "URLS.length") == 6 and js(ctx, "polls()") == [45000]


def test_a_one_page_list_polls_as_before(ctx):
    ctx.eval("TOTAL = 50; MR.mount(Parent, { show: true, k: 'a' });")
    drain(ctx)
    assert js(ctx, "URLS.length") == 1 and js(ctx, "polls()") == [15000]


def test_a_long_list_polls_at_most_every_minute(ctx):
    """Round 2, finding 5: 1600 rows are 8 pages, which at 15 s each would be two minutes between reads; a minute is the most."""
    ctx.eval("TOTAL = 1600; MR.mount(Parent, { show: true, k: 'a' });")
    drain(ctx)
    assert js(ctx, "shown()") == "1600" and js(ctx, "URLS.length") == 8
    assert js(ctx, "polls()") == [60000]


def test_unmounting_mid_read_aborts_it_and_stops(ctx):
    ctx.eval("MR.mount(Parent, { show: true, k: 'a' });")
    ctx.eval("answerOne();")  # page 1 answered; page 2 asked for
    assert js(ctx, "URLS.length") == 2 and js(ctx, "PENDING.length") == 1
    ctx.eval("MR.rerender({ show: false, k: 'a' }); MR.rerender({ show: false, k: 'a' });")
    assert js(ctx, "URLS.length") == 2 and js(ctx, "TIMERS.length") == 0
    assert js(ctx, "window.primerApi._resource.findKeys('a')") == []
    ctx.eval("answerOne(); MR.rerender({ show: false, k: 'a' });")
    assert js(ctx, "URLS.length") == 2


def test_a_key_change_mid_read_abandons_the_old_read(ctx):
    ctx.eval("MR.mount(Parent, { show: true, k: 'a' });")
    ctx.eval("answerOne();")
    ctx.eval("MR.rerender({ show: true, k: 'b' });")
    drain(ctx)
    assert len(js(ctx, "URLS")) == 5, "'a' asked for two pages, 'b' read its three"
    assert js(ctx, "window.primerApi._resource.findKeys('a')") == []
    assert js(ctx, "polls()") == [45000]


def test_a_first_read_cut_short_shows_what_it_read_with_the_note(ctx):
    ctx.eval("FAIL_AT = 400; MR.mount(Parent, { show: true, k: 'a' });")
    drain(ctx)
    assert js(ctx, "shown()") == "400"
    assert ctx.eval("MR.find('note').children.join('')") == "Showing the first 400 agents; the rest did not load: blip"


def test_a_refresh_cut_short_keeps_the_whole_list_and_reports_the_failure(ctx):
    """Round 2, finding 6: a poll whose third page fails no longer swaps the 600 rows for 400 and a note; the list stays (stale-while-error), the
    failure is the resource's error, and the next poll reads it whole again."""
    ctx.eval("MR.mount(Parent, { show: true, k: 'a' });")
    drain(ctx)
    assert js(ctx, "shown()") == "600"
    ctx.eval("FAIL_AT = 400; TIMERS[0].fn();")
    drain(ctx)
    assert js(ctx, "shown()") == "600" and js(ctx, "MR.find('note')") is None
    assert js(ctx, "LAST.error && LAST.error.status") == 503
    ctx.eval("FAIL_AT = null; TIMERS[0].fn();")
    drain(ctx)
    assert js(ctx, "shown()") == "600" and js(ctx, "LAST.error") is None
