"""The toolset Sessions tab selects its rows server-side (ticket 01a16eaa).

Before: the tab read every page of ``GET /sessions`` and every page of ``GET /agents`` (``PR_fetchAllPages``) and joined them
client-side (the sessions whose bound agent's ``tools`` includes this id). On the 450-session / 230-agent table this test seeds,
that is FIVE whole-table page requests for a table of three rows. Now the tab asks the server's filter queries:
``POST /agents/find`` with the ``tools CONTAINS <id>`` predicate, then ``POST /sessions/find`` with ``binding.agent_id IN
<the ids that came back>``, newest first, via the foundation's ``PR_fetchAllFind`` (``ui/foundation/api.js``) - one request
per page of the answer, and a cut-short answer is said under the list (``PR_PagedNote``), never silent. The fake ``apiFetch``
answers BOTH the find routes and the old whole-table list routes, so the pre-fix code runs to completion and the request
pattern (not a crash) is what fails red.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
TOOLSETS = UI / "components" / "toolsets.jsx"
API = (UI / "foundation" / "api.js").read_text(encoding="utf-8")

PRELUDE = r"""
var CALLS = [], KEYS = [], NAVS = [];
var RES = {};
var SIGNAL = { aborted: false };
// The fake tables: 230 agents (two reference the "misc" toolset) and 450 sessions (three are bound to those agents),
// so the OLD read - every page of GET /sessions and GET /agents - costs five requests, the NEW one costs two.
var AGENTS = []; for (var i = 0; i < 230; i++) AGENTS.push({ id: "ag-" + i, tools: i < 2 ? ["misc"] : [], created_at: "2026-01-01T00:00:00Z" });
var SESSIONS = []; for (var i = 0; i < 450; i++) {
  var bound = i === 0 ? "ag-0" : (i === 150 ? "ag-1" : (i === 300 ? "ag-0" : null));
  SESSIONS.push({ id: "s-" + i, binding: bound ? { agent_id: bound } : { kind: "graph", graph_id: "g-" + (i % 7) },
    status: "running", workspace_id: "w1", created_at: "2026-01-0" + (1 + (i % 9)) + "T00:00:00Z" });
}
function pageOf(rows, offset, length, desc) {
  var r = rows.slice();
  if (desc) r.sort(function (a, b) { return a.created_at < b.created_at ? 1 : -1; });
  var items = r.slice(offset, offset + length);
  return Promise.resolve({ kind: "offset", offset: offset, length: items.length, total: rows.length, items: items });
}
// The real api.js (eval'd before this prelude) owns ApiError and readRefusal; the stubs below take only the keys the
// component reads - useResource (driven like the real hook: once per resource, a refetch re-runs it, re-rendering when
// the answer lands), useRouter and apiFetch (the fake server) - and leave the rest of the namespace alone.
window.primerApi.useResource = function (key, fetcher, opts) {
    KEYS.push(key);
    var e = RES[key] || (RES[key] = { data: undefined, error: null, loading: true, ran: false, refetched: 0 });
    function run() {
      e.loading = true; e.error = null;
      var p; try { p = fetcher(SIGNAL); } catch (err) { p = Promise.reject(err); }
      p.then(function (data) { e.data = data; e.loading = false; MR.rerender(); },
             function (err) { e.error = err; e.loading = false; MR.rerender(); });
    }
    if (!e.ran) { e.ran = true; run(); }
    return { data: e.data, error: e.error, loading: e.loading, refetch: function () { e.refetched += 1; e.ran = false; run(); } };
  };
window.primerApi.useRouter = function () { return { navigate: function (path, opts) { NAVS.push(path); } }; };
window.primerApi.apiFetch = function (method, path, body, o) {
    CALLS.push({ method: method, path: path, body: body == null ? null : JSON.parse(JSON.stringify(body)) });
    if (window.__failAt != null && body && body.page && body.page.offset >= window.__failAt) {
      return Promise.reject(new window.primerApi.ApiError({ status: 503, title: "Service Unavailable", detail: "blip" }));
    }
    if (method === "POST" && path === "/agents/find") {
      var tid = body.predicate.right.value;
      return pageOf(AGENTS.filter(function (a) { return a.tools.indexOf(tid) >= 0; }), body.page.offset, body.page.length);
    }
    if (method === "POST" && path === "/sessions/find") {
      var ids = body.predicate.right.value;
      var rows = SESSIONS.filter(function (s) { return s.binding && s.binding.agent_id != null && ids.indexOf(s.binding.agent_id) >= 0; });
      return pageOf(rows, body.page.offset, body.page.length, body.order_by != null);
    }
    if (method === "GET") {
      // The whole-table list routes (the OLD read): answered so the pre-fix code runs to completion.
      var at = path.indexOf("?"); var base = at < 0 ? path : path.slice(0, at);
      var lm = /limit=(\d+)/.exec(path), om = /offset=(\d+)/.exec(path);
      var limit = lm ? Number(lm[1]) : 200, offset = om ? Number(om[1]) : 0;
      if (base === "/sessions") return pageOf(SESSIONS, offset, limit, /order_by=created_at:desc/.test(path));
      if (base === "/agents") return pageOf(AGENTS, offset, limit);
      return Promise.resolve({ kind: "offset", offset: offset, length: 0, total: 0, items: [] });
    }
    return Promise.resolve({});
  };
function Btn(p) { return React.createElement("button", Object.assign({}, p), p.children); }
function Banner(p) { return React.createElement("div", { "data-testid": "banner" }, p.title, p.detail, p.actions); }
"""


@pytest.fixture
def ctx():
    c = mini_react_context(transpile(TOOLSETS), API + "\n" + PRELUDE)
    try:
        yield c
    finally:
        c.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def _mount(ctx, toolset_id: str = "misc") -> None:
    ctx.eval(f"MR.mount(TS_SessionsTab, {{ id: {json.dumps(toolset_id)} }});")


def _calls(ctx) -> list:
    return js(ctx, "CALLS")


def _flat(ctx) -> str:
    """The rendered text with all whitespace stripped: JSX interpolations are separate text nodes
    (``(2 agents)`` draws as ``( 2  agent s )``), so a phrase is checked whitespace-free."""
    return re.sub(r"\s+", "", " ".join(js(ctx, "MR.texts()")))


def test_the_tab_asks_the_servers_filter_queries_not_the_tables(ctx):
    """Two find requests - the agents that reference the id, then the sessions bound to them - and no whole-table read."""
    _mount(ctx)
    calls = _calls(ctx)
    assert [(c["method"], c["path"]) for c in calls] == [("POST", "/agents/find"), ("POST", "/sessions/find")]
    agents_body = calls[0]["body"]
    assert agents_body["predicate"] == {
        "kind": "predicate",
        "left": {"kind": "field", "name": "tools"},
        "op": "contains",
        "right": {"kind": "value", "value": "misc"},
    }
    sessions_body = calls[1]["body"]
    assert sessions_body["predicate"]["left"] == {"kind": "field", "name": "binding.agent_id"}
    assert sessions_body["predicate"]["op"] == "in"
    assert sessions_body["predicate"]["right"]["value"] == ["ag-0", "ag-1"]
    assert sessions_body["order_by"] == [{"field": "created_at", "direction": "desc"}]


def test_the_table_shows_only_the_sessions_bound_to_the_agents_that_use_the_id(ctx):
    """The three bound sessions, newest first (s-150 created on the 7th, s-300 on the 4th, s-0 on the 1st); the 447 others never show."""
    _mount(ctx)
    nodes = js(ctx, "MR.texts()")
    for sid in ("s-150", "s-300", "s-0"):
        assert sid in nodes
    # Exact text-node membership, not substring: the 447 unbound sessions never show (s-1 is a substring of s-150).
    for other in ("s-1", "s-149", "s-151", "s-299", "s-301", "s-449"):
        assert other not in nodes


def test_the_count_line_says_the_agents_that_reference_the_id(ctx):
    _mount(ctx)
    assert "(2agents)" in _flat(ctx)


def test_a_toolset_no_agent_uses_makes_one_find_and_no_sessions_read(ctx):
    """No agents reference the id, so the id set is empty and the sessions are never asked for at all."""
    _mount(ctx, toolset_id="web")
    calls = _calls(ctx)
    assert [(c["method"], c["path"]) for c in calls] == [("POST", "/agents/find")]
    flat = _flat(ctx)
    assert "(0agents)" in flat
    assert "Nosessionsreferencethistoolset." in flat


def test_the_tab_reads_every_page_of_the_answer(ctx):
    """250 matching sessions: one request per page of the ANSWER (two pages of 200/50), not of the 450-row table."""
    ctx.eval(
        "SESSIONS = [];"
        "for (var i = 0; i < 250; i++) SESSIONS.push({ id: 's-' + i, binding: { agent_id: i % 2 === 0 ? 'ag-0' : 'ag-1' },"
        "  status: 'running', workspace_id: 'w1', created_at: '2026-01-0' + (1 + (i % 9)) + 'T00:00:00Z' });"
    )
    _mount(ctx)
    calls = _calls(ctx)
    assert [(c["method"], c["path"]) for c in calls] == [
        ("POST", "/agents/find"),
        ("POST", "/sessions/find"),
        ("POST", "/sessions/find"),
    ]
    assert [c["body"]["page"]["offset"] for c in calls[1:]] == [0, 200]
    texts = " ".join(js(ctx, "MR.texts()"))
    assert "s-249" in texts


def test_a_cut_short_answer_is_said_under_the_list(ctx):
    """The second page of the sessions answer fails: the first 200 stay, and the note says the rest did not load."""
    ctx.eval(
        "SESSIONS = [];"
        "for (var i = 0; i < 250; i++) SESSIONS.push({ id: 's-' + i, binding: { agent_id: 'ag-0' }, status: 'running',"
        "  workspace_id: 'w1', created_at: '2026-01-0' + (1 + (i % 9)) + 'T00:00:00Z' });"
        "window.__failAt = 200;"
    )
    _mount(ctx)
    note = js(ctx, "MR.find('ts-sessions-note')")
    assert note is not None
    texts = " ".join(js(ctx, "MR.texts()"))
    assert "Showing the first 200 sessions; the rest did not load: blip" in texts


def test_no_note_for_a_whole_answer(ctx):
    _mount(ctx)
    assert js(ctx, "MR.find('ts-sessions-note')") is None
    assert js(ctx, "MR.find('ts-agents-note')") is None
