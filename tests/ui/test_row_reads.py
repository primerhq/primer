"""``PR_useRowReads`` (ui/foundation/api.js): the agents and graphs pages read each row's status once, not once per row on every list change (review 8.3).

Runs the real hook through the mini-React harness with reads the test answers by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
API = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")
AGENTS = (ROOT / "ui" / "components" / "agents.jsx").read_text(encoding="utf-8")
GRAPHS = (ROOT / "ui" / "components" / "graphs.jsx").read_text(encoding="utf-8")

_PRELUDE = r"""
function AbortController() {
  var s = { aborted: false };
  this.signal = s;
  this.abort = function () { s.aborted = true; };
}
var READS = []; var PEND = []; var LIVE = 0; var PEAK = 0; var LAST = null;
function readRow(row, signal) {
  READS.push(row.id);
  LIVE += 1; PEAK = Math.max(PEAK, LIVE);
  return new Promise(function (resolve) { PEND.push(function () { LIVE -= 1; resolve("ok:" + row.id + ":" + (row.v || 0)); }); });
}
function answerAll() { var n = 0; while (PEND.length) { PEND.shift()(); n++; } return n; }
function Host(props) { LAST = PR_useRowReads(props.items, readRow); return React.createElement("i", null, "x"); }
"""


@pytest.fixture
def ctx():
    c = mini_react_context(API, _PRELUDE)
    try:
        yield c
    finally:
        c.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def _settle(ctx) -> None:
    for _ in range(4):
        ctx.eval("answerAll(); MR.rerender();")


def _rows(n: int, v: int = 0) -> str:
    return "[" + ",".join(f'{{id:"a{i}",v:{v}}}' for i in range(n)) + "]"


def test_reads_are_capped_at_four_in_flight_and_every_row_is_answered(ctx):
    ctx.eval("MR.mount(Host, { items: " + _rows(10) + " });")
    ctx.eval("MR.rerender();")
    assert js(ctx, "PEAK") <= 4 and js(ctx, "READS.length") == 4
    _settle(ctx)
    assert js(ctx, "Object.keys(LAST.byId).length") == 10
    assert js(ctx, "LAST.byId.a3") == "ok:a3:0"


def test_a_list_change_reads_only_the_rows_that_changed(ctx):
    ctx.eval("MR.mount(Host, { items: " + _rows(5) + " });")
    _settle(ctx)
    ctx.eval("READS.length = 0;")
    # The same page comes back as a NEW payload (another poll, an edit to a row on another page): nothing is read again.
    ctx.eval("MR.rerender({ items: " + _rows(5) + " });")
    _settle(ctx)
    assert js(ctx, "READS") == []
    # One row changed, one row is new: only those two are read.
    ctx.eval('MR.rerender({ items: [{id:"a0",v:0},{id:"a1",v:9},{id:"a2",v:0},{id:"a3",v:0},{id:"a4",v:0},{id:"a5",v:0}] });')
    _settle(ctx)
    assert sorted(js(ctx, "READS")) == ["a1", "a5"]
    assert js(ctx, "LAST.byId.a1") == "ok:a1:9" and js(ctx, "LAST.byId.a0") == "ok:a0:0"


def test_a_row_that_leaves_the_page_drops_its_answer_and_an_empty_page_does_not_loop(ctx):
    ctx.eval("MR.mount(Host, { items: " + _rows(3) + " });")
    _settle(ctx)
    ctx.eval("MR.rerender({ items: " + _rows(1) + " });")
    _settle(ctx)
    assert js(ctx, "Object.keys(LAST.byId)") == ["a0"]
    ctx.eval("MR.rerender({ items: [] });")  # a fresh [] every render: the hook must settle (the harness throws on a render loop)
    _settle(ctx)
    assert js(ctx, "Object.keys(LAST.byId)") == []


def test_forget_reads_every_row_again(ctx):
    ctx.eval("MR.mount(Host, { items: " + _rows(3) + " });")
    _settle(ctx)
    ctx.eval("READS.length = 0; LAST.forget(); MR.rerender();")
    _settle(ctx)
    assert sorted(js(ctx, "READS")) == ["a0", "a1", "a2"]


def test_the_agents_and_graphs_pages_use_it_and_refresh_forgets():
    # The pages are wired to the hook (no per-list-change fan-out of their own); their Refresh verb forgets the answers.
    for src in (AGENTS, GRAPHS):
        assert "PR_useRowReads(items" in src
        assert ".forget()" in src
        assert "PR_mapLimit(" not in src
