"""The harnesses detail panels select their rows server-side (ticket 01a16eaa).

Before: ``HR_ManagedObjects`` and ``HR_DependenciesPanel`` each read every page of the five whole tables
(``GET /agents``, ``/graphs``, ``/collections``, ``/documents``, ``/toolsets``; ``PR_fetchAllPages``) and kept only the rows
with ``harness_id === <this harness>`` client-side - so the detail page made the whole-table read TWICE. On the tables this
test seeds (230 agents, 400 graphs, 150 documents, 30 toolsets, 20 collections) that is SEVEN page requests per panel for a
handful of chips. Now each panel asks the server's filter query: ``POST <path>/find`` with the ``harness_id = <id>``
predicate, via the foundation's ``PR_fetchAllFind`` (``ui/foundation/api.js``) - one request per page of the answer. The
fake ``apiFetch`` answers BOTH the find routes and the old whole-table list routes, so the pre-fix code runs to completion
and the request pattern (not a crash) is what fails red.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
HARNESSES = UI / "components" / "harnesses.jsx"
API = (UI / "foundation" / "api.js").read_text(encoding="utf-8")

# The fake tables, keyed by the list routes' bases: 230 agents (3 of them the harness's, one sourced from "dep-a"),
# 400 graphs (1), 150 documents (1), 30 toolsets (1), 20 collections (2, one sourced from "dep-a").
FAKE_TABLES = """
var TABLES = {
  "/agents": (function () { var r = []; for (var i = 0; i < 230; i++) r.push({ id: "ag-" + i, harness_id: i < 3 ? "h1" : null,
    source_dependency: i === 2 ? "dep-a" : null, created_at: "2026-01-01T00:00:00Z" }); return r; })(),
  "/graphs": (function () { var r = []; for (var i = 0; i < 400; i++) r.push({ id: "g-" + i, harness_id: i === 7 ? "h1" : null,
    created_at: "2026-01-01T00:00:00Z" }); return r; })(),
  "/collections": (function () { var r = []; for (var i = 0; i < 20; i++) r.push({ id: "c-" + i, harness_id: i < 2 ? "h1" : null,
    source_dependency: i === 1 ? "dep-a" : null, created_at: "2026-01-01T00:00:00Z" }); return r; })(),
  "/documents": (function () { var r = []; for (var i = 0; i < 150; i++) r.push({ id: "d-" + i, harness_id: i === 4 ? "h1" : null,
    created_at: "2026-01-01T00:00:00Z" }); return r; })(),
  "/toolsets": (function () { var r = []; for (var i = 0; i < 30; i++) r.push({ id: "t-" + i, harness_id: i === 1 ? "h1" : null,
    created_at: "2026-01-01T00:00:00Z" }); return r; })(),
};
"""

PRELUDE = r"""
var CALLS = [], KEYS = [], RES = {};
var SIGNAL = { aborted: false };
// The real api.js (eval'd before this prelude) owns ApiError and readRefusal; the stubs below take only the keys the
// panels read - useResource (driven like the real hook: once per resource, a refetch re-runs it, re-rendering when the
// answer lands) and apiFetch (the fake server) - and leave the rest of the namespace alone.
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
window.primerApi.apiFetch = function (method, path, body, o) {
  CALLS.push({ method: method, path: path, body: body == null ? null : JSON.parse(JSON.stringify(body)) });
  if (method === "POST" && path.slice(-5) === "/find") {
    var table = TABLES[path.slice(0, -5)] || [];
    var hid = body.predicate.right.value;
    var rows = table.filter(function (row) { return row.harness_id === hid; });
    var items = rows.slice(body.page.offset, body.page.offset + body.page.length);
    return Promise.resolve({ kind: "offset", offset: body.page.offset, length: items.length, total: rows.length, items: items });
  }
  if (method === "GET") {
    // The whole-table list routes (the OLD read): answered so the pre-fix code runs to completion.
    var at = path.indexOf("?"); var base = at < 0 ? path : path.slice(0, at);
    var lm = /limit=(\d+)/.exec(path), om = /offset=(\d+)/.exec(path);
    var limit = lm ? Number(lm[1]) : 200, offset = om ? Number(om[1]) : 0;
    var all = TABLES[base] || [];
    var items = all.slice(offset, offset + limit);
    return Promise.resolve({ kind: "offset", offset: offset, length: items.length, total: all.length, items: items });
  }
  return Promise.resolve({});
};
function Icon(p) { return null; }
function Btn(p) { return React.createElement("button", Object.assign({}, p), p.children); }
function Banner(p) { return React.createElement("div", { "data-testid": "banner" }, p.title, p.detail, p.actions); }
"""


@pytest.fixture
def ctx():
    c = mini_react_context(transpile(HARNESSES), API + "\n" + FAKE_TABLES + "\n" + PRELUDE)
    try:
        yield c
    finally:
        c.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def _calls(ctx) -> list:
    return js(ctx, "CALLS")


def _find_paths(calls: list) -> list:
    return [c["path"] for c in calls]


def _flat(ctx) -> str:
    """The rendered text with all whitespace stripped: JSX interpolations are separate text nodes
    (``{count} entities`` draws as ``2  entities``), so a phrase is checked whitespace-free."""
    return re.sub(r"\s+", "", " ".join(js(ctx, "MR.texts()")))


def test_managed_objects_asks_five_find_queries_not_the_tables(ctx):
    """One POST /<plural>/find per endpoint - five requests for tables of 230/400/150/30/20 rows - and no whole-table read."""
    ctx.eval("MR.mount(HR_ManagedObjects, { harnessId: 'h1', slug: 'h1' });")
    calls = _calls(ctx)
    assert [c["method"] for c in calls] == ["POST"] * 5
    assert _find_paths(calls) == [
        "/agents/find", "/graphs/find", "/collections/find", "/documents/find", "/toolsets/find",
    ]
    for c in calls:
        assert c["body"]["predicate"] == {
            "kind": "predicate",
            "left": {"kind": "field", "name": "harness_id"},
            "op": "=",
            "right": {"kind": "value", "value": "h1"},
        }
        assert c["body"]["page"]["offset"] == 0


def test_managed_objects_shows_only_the_harnesses_rows(ctx):
    """The harness's own rows - 3 agents, 1 graph, 2 collections, 1 document, 1 toolset, 8 total - and none of the rest."""
    ctx.eval("MR.mount(HR_ManagedObjects, { harnessId: 'h1', slug: 'h1' });")
    nodes = js(ctx, "MR.texts()")
    for chip in ("ag-0", "ag-1", "ag-2", "g-7", "c-0", "c-1", "d-4", "t-1"):
        assert chip in nodes
    for other in ("ag-3", "ag-229", "g-0", "c-2", "d-0", "t-0"):
        assert other not in nodes
    assert "8total" in _flat(ctx)


def test_the_dependencies_counts_come_from_find_queries_too(ctx):
    """The informational count badge is the same five finds (hooks run even though the panel renders nothing else):
    two of the harness's rows carry source_dependency "dep-a", so the badge says "2 entities"."""
    ctx.eval(
        "MR.mount(HR_DependenciesPanel, { harness: { id: 'h1', dependencies_resolved: [ { name: 'dep-a', slug: 'dep-a' } ] } });"
    )
    calls = _calls(ctx)
    assert [c["method"] for c in calls] == ["POST"] * 5
    assert _find_paths(calls) == [
        "/agents/find", "/graphs/find", "/collections/find", "/documents/find", "/toolsets/find",
    ]
    texts = " ".join(js(ctx, "MR.texts()"))
    assert "2entities" in _flat(ctx)
    assert "dep-a" in texts


def test_a_dependencies_panel_with_no_deps_still_makes_the_reads_but_renders_nothing(ctx):
    """Hooks run unconditionally before the early return: the five finds still go out (the read the panel owns), and the panel draws nothing."""
    ctx.eval("MR.mount(HR_DependenciesPanel, { harness: { id: 'h1', dependencies_resolved: [] } });")
    calls = _calls(ctx)
    assert len(calls) == 5 and all(c["method"] == "POST" for c in calls)
    assert js(ctx, "MR.texts()") == []
