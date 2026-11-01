"""The Activity panel applies the filters in force at the last refresh and drops stale answers (UI-L3).

``SH_ActivityPanel`` (``ui/components/shell/sh-activity.jsx``) read the filter boxes at the moment of each ``query`` and
appended Load More's answer to the ``rows`` it had closed over: a Refresh overlapping a Load More (or another Refresh)
resolved in either order, and the older answer - fetched under the earlier filter - replaced or re-appended over the
newer one. The panel now keeps the applied filters in a ref set by ``refresh``, numbers each request, and ignores an
answer that is not the latest one's.

MiniRacer on the real file with deferred ``SH_api.events`` answers: two overlapping answers resolved in reverse order
must leave the latest request's rows on screen, a Load More must query the refresh's filters (not the boxes as edited),
and in-order answers keep their characterisation (filters apply on an explicit refresh; Load More appends).

Every ``SH_api.events`` call is a deferred answer the test resolves by hand: a refresh issues the probe first
(``{ limit: 1 }``) and the window query only when the probe answers, so the probe and the window are separate entries
in the recorded list and the window query is the call the filters apply to.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
ACTIVITY = ROOT / "ui" / "components" / "shell" / "sh-activity.jsx"
API_JS = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")

PRELUDE = r"""
var window = globalThis;
var EVENTS = [];   // every SH_api.events call: { opts, resolve }
var TOASTS = [];
window.SH_api = {
  events: function (opts) {
    var d;
    var p = new Promise(function (res) { d = res; });
    EVENTS.push({ opts: opts, resolve: d });
    return p;
  },
  adminUsers: function () { return Promise.resolve({ items: [] }); },
  eventSubscriptions: function () { return Promise.resolve({ items: [] }); },
};
window.primerApi = {
  toastPush: function (t) { TOASTS.push(t); },
};
"""

DRIVER = r"""
function __rows(ids) {
  return ids.map(function (id) {
    return { id: id, event_type: "session.event", entity_kind: "session",
             entity_id: "s" + id, actor: "system", session_id: "s" + id,
             occurred_at: "2026-11-01T00:00:00Z" };
  });
}
function __rowIds() {
  var out = [];
  MR.findAll("activity-row:").forEach(function (el) {
    out.push(parseInt(el.props["data-testid"].slice("activity-row:".length), 10));
  });
  return out;
}
"""


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _first_window(ctx, ids) -> None:
    """Mount's refresh: the probe answers (EVENTS[0]), then the window query (EVENTS[1]) resolves with the given rows."""
    ctx.eval("EVENTS[0].resolve({ max_id: 500 });")
    ctx.eval("MR.rerender();")
    ctx.eval("EVENTS[1].resolve({ items: __rows(" + json.dumps(ids) + "), max_id: 500 });")
    ctx.eval("MR.rerender();")


@pytest.fixture
def ctx() -> Iterator[object]:
    c = mini_react_context(transpile(ACTIVITY), PRELUDE)
    c.eval(API_JS)  # the real foundation: PR_PagedNote and PR_partialFailure (the subscriptions table renders one)
    c.eval(DRIVER)
    c.eval("MR.mount(SH_ActivityPanel, {});")
    try:
        yield c
    finally:
        c.close()


def test_a_filter_refresh_and_an_in_order_answer_are_shown(ctx) -> None:
    # Characterisation: typing a filter and pressing Refresh queries it (the window
    # query of that refresh carries it), and the in-order answer replaces the rows.
    _first_window(ctx, [401, 402, 403])
    ctx.eval(
        'MR.find("activity-filter:eventType").props.onChange({ target: { value: "session.*" } });'
    )
    ctx.eval("MR.rerender();")
    ctx.eval('MR.click("activity-refresh");')
    ctx.eval("EVENTS[2].resolve({ max_id: 500 });")  # the refresh's probe -> its window query (EVENTS[3])
    ctx.eval("MR.rerender();")
    assert _js(ctx, "EVENTS[3].opts.eventType") == "session.*", "the refresh must query the typed filter"
    ctx.eval("EVENTS[3].resolve({ items: __rows([501, 502]), max_id: 500 });")
    ctx.eval("MR.rerender();")
    assert _js(ctx, "__rowIds()") == [501, 502], "the refresh's answer must replace the rows"


def test_a_load_more_in_order_appends_the_older_window(ctx) -> None:
    # Characterisation: with the answers in order, Load More paginates from the oldest
    # row shown and appends its window to the rows already there.
    _first_window(ctx, [401, 402, 403])
    ctx.eval('MR.click("activity-load-more");')
    assert _js(ctx, "EVENTS[2].opts.afterId") == 403, "Load More paginates from the oldest row shown"
    ctx.eval("EVENTS[2].resolve({ items: __rows([404, 405]), max_id: 500 });")
    ctx.eval("MR.rerender();")
    assert _js(ctx, "__rowIds()") == [401, 402, 403, 404, 405], "the Load More answer must append"


def test_a_load_more_after_a_box_edit_queries_the_applied_filter(ctx) -> None:
    # Red (UI-L3): the box was edited without Enter; Load More must query the
    # filters in force at the last refresh, not the edited box.
    _first_window(ctx, [401, 402, 403])
    ctx.eval(
        'MR.find("activity-filter:eventType").props.onChange({ target: { value: "session.*" } });'
    )
    ctx.eval("MR.rerender();")
    ctx.eval('MR.click("activity-load-more");')
    assert _js(ctx, "EVENTS[2].opts.eventType") is None, (
        "Load More mixed the edited box into a window read under the applied filter"
    )
    ctx.eval("EVENTS[2].resolve({ items: __rows([404, 405]), max_id: 500 });")
    ctx.eval("MR.rerender();")
    assert _js(ctx, "__rowIds()") == [401, 402, 403, 404, 405]


def test_out_of_order_answers_keep_the_latest_request_rows(ctx) -> None:
    # Red (UI-L3): a second Refresh overlaps the first (Q1 from the mount's refresh,
    # Q2 from the clicked one); the answers resolve in reverse order and the stale
    # one must not replace the latest request's rows.
    ctx.eval("EVENTS[0].resolve({ max_id: 500 });")  # mount's probe -> Q1 (EVENTS[1])
    ctx.eval("MR.rerender();")
    ctx.eval('MR.click("activity-refresh");')        # overlapping probe (EVENTS[2])
    ctx.eval("EVENTS[2].resolve({ max_id: 500 });")  # -> Q2 (EVENTS[3]), the latest request
    ctx.eval("MR.rerender();")
    # Q2 (the latest request) answers first...
    ctx.eval("EVENTS[3].resolve({ items: __rows([901, 902]), max_id: 500 });")
    ctx.eval("MR.rerender();")
    # ...then Q1's stale answer lands.
    ctx.eval("EVENTS[1].resolve({ items: __rows([801, 802]), max_id: 500 });")
    ctx.eval("MR.rerender();")
    assert _js(ctx, "__rowIds()") == [901, 902], (
        "an older request's answer replaced the latest request's rows"
    )
