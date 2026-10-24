"""The shell's lists read every page (ticket 01a16dfa): the rail's workspaces, the palette's entities and the phone's Spaces tab.

After #911 removed the paged Workspaces page, a workspace past the first 200 was reachable nowhere in the console: the rail, the phone's Spaces tab and
the palette all read ``SH_api.workspaces``, which asked for one page of 200. They run here on the hook runtime with the REAL ``ui/foundation/api.js``
(``PR_fetchAllPages``, ``PR_usePagedResource``, ``PR_PagedNote``), the REAL ``ui/foundation/use-resource.js`` (its cache, its polls: timers are recorded in
``TIMERS``, not run) and the REAL ``ui/components/shell/sh-api.jsx``, against a fake ``apiFetch`` that pages the way the server does (``limit`` and
``offset``, a ``total``) and answers ``POST /sessions/find`` newest first, filtered on a ``workspace_id`` predicate, as the server does.

Round 2 of the review: the rail and the Spaces tab counted a workspace's sessions in the newest 200 sessions of the whole install, so on any install with
more, most of the workspaces this ticket newly lists said "0 sessions". The count is now the workspace row's own ``session_count`` (exact, every status),
and an open workspace reads its own sessions (``SH_api.allSessions`` with the wid).
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
API = (UI / "foundation" / "api.js").read_text(encoding="utf-8")
USE_RESOURCE = (UI / "foundation" / "use-resource.js").read_text(encoding="utf-8")
SH_API = UI / "components" / "shell" / "sh-api.jsx"
RAIL = UI / "components" / "console" / "nv-rail.jsx"
PALETTE = UI / "components" / "console" / "nv-palette.jsx"
MOBILE = UI / "components" / "console" / "nv-mobile-shell.jsx"

_PRELUDE = r"""
var window = globalThis;
window.innerWidth = 1280; window.innerHeight = 800;
var TIMERS = []; var TID = 0;
function setTimeout(fn, ms) { TID += 1; TIMERS.push({ id: TID, fn: fn, ms: ms }); return TID; }
function clearTimeout(id) { TIMERS = TIMERS.filter(function (t) { return t.id !== id; }); }
function AbortController() { var s = { aborted: false }; this.signal = s; this.abort = function () { s.aborted = true; }; }
var document = { hidden: false, title: "", addEventListener: function () {}, removeEventListener: function () {}, body: { style: {} } };
window.addEventListener = function () {}; window.removeEventListener = function () {};
function NV_identity() { return { d: "", color: "" }; }
window.useWorkspaceTapListener = function () {};
var CON = { paletteRef: { current: {} }, wid: "w-0", registry: [], doc: null, frecency: null, workspaces: [], workspacesList: null };
function NV_useConsole() { return CON; }
function SH_rankVerbs() { return []; }
"""

# The server: COUNTS[name] rows of each list, `<prefix>-<n>` in id order, FAIL_FROM[name] the first offset that answers 500 (a page that fails mid-way).
# SESSIONS: 350, newest first: s-0 .. s-249 on w-0, s-250 .. s-349 (all older than the newest 200 of the install) on w-1; each workspace row carries
# its session_count, as GET /v1/workspaces does.
_SERVER = r"""
var COUNTS = { workspaces: 450, agents: 450, graphs: 3, toolsets: 0 };
var PREFIX = { workspaces: "w", agents: "agent", graphs: "graph", toolsets: "ts" };
var FAIL_FROM = {};
var CALLS = [];
var SESSIONS = [];
for (var k = 0; k < 350; k++) SESSIONS.push({ id: "s-" + k, workspace_id: k < 250 ? "w-0" : "w-1", status: "ended", binding: { kind: "agent", agent_id: "a" },
  created_at: new Date(Date.UTC(2026, 9, 24) - k * 60000).toISOString() });
function sessionCount(wid) { return SESSIONS.filter(function (s) { return s.workspace_id === wid; }).length; }
window.primerApi.apiFetch = function (method, path, body) {
  CALLS.push(method + " " + path + (body && body.predicate ? " " + body.predicate.right.value : ""));
  if (method === "POST" && path === "/sessions/find") {
    var rows = body.predicate ? SESSIONS.filter(function (s) { return s.workspace_id === body.predicate.right.value; }) : SESSIONS;
    return Promise.resolve({ kind: "offset", items: rows.slice(body.page.offset, body.page.offset + body.page.length), total: rows.length });
  }
  var m = /^\/(workspaces|agents|graphs|toolsets)\?limit=(\d+)&offset=(\d+)$/.exec(path);
  if (m) {
    var name = m[1], limit = Number(m[2]), offset = Number(m[3]);
    if (FAIL_FROM[name] != null && offset >= FAIL_FROM[name]) {
      return Promise.reject(new window.primerApi.ApiError({ status: 500, title: "Internal Server Error", detail: "the store is down" }));
    }
    var n = Math.max(0, Math.min(limit, COUNTS[name] - offset)), items = [];
    for (var i = 0; i < n; i++) {
      var id = PREFIX[name] + "-" + (offset + i);
      items.push({ id: id, name: "Workspace " + (offset + i), session_count: sessionCount(id) });
    }
    return Promise.resolve({ kind: "offset", offset: offset, length: n, total: COUNTS[name], items: items });
  }
  return Promise.resolve({ items: [] });
};
window.primerApi.useEscape = function () {};
window.primerApi.useFocusTrap = function () {};
function ids(prefix) { return MR.findAll(prefix).map(function (e) { return e.props["data-testid"].slice(prefix.length); }); }
function textOf(n) {
  var out = [];
  (function walk(x) {
    if (x == null || typeof x === "boolean") return;
    if (typeof x === "string" || typeof x === "number") { out.push(String(x)); return; }
    if (Array.isArray(x)) { x.forEach(walk); return; }
    if (x.__el) walk(typeof x.type === "function" && x.type !== React.Fragment ? x.out : x.children);
  })(n);
  return out.join("");
}
"""


@cache
def _code(*files: Path) -> str:
    return API + "\n" + USE_RESOURCE + "\n" + "\n".join(transpile(f) for f in files)


def _ctx(*files: Path, extra: str = ""):
    ctx = mini_react_context(_code(*files), _PRELUDE + extra)
    ctx.eval(_SERVER)
    return ctx


@pytest.fixture
def rail():
    c = _ctx(SH_API, RAIL)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def palette():
    c = _ctx(SH_API, PALETTE)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def spaces():
    c = _ctx(SH_API, MOBILE, extra="window.NV_sessionStateChipView = function () { return {}; };"
             " window.Fab = function () { return null; }; window.BottomSheet = function () { return null; };")
    try:
        yield c
    finally:
        c.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def settle(ctx) -> None:
    """Draw what the reads answered: each eval ends with V8's microtask queue drained, so a paged read's chain of pages lands between two evals, and
    the render after it draws the rows (and the effect that lengthens the poll runs)."""
    for _ in range(6):
        ctx.eval("MR.rerender();")


# ---- the rail -------------------------------------------------------------------------------------------------------------------------------------------


def test_the_rail_lists_every_workspace_past_200(rail):
    rail.eval('MR.mount(NV_Rail, { selectedWorkspaceId: "w-449" });')
    settle(rail)
    shown = js(rail, "ids('nv-rail-ws:')")
    assert len(shown) == 450 and shown[0] == "w-0" and shown[-1] == "w-449"
    assert [c for c in js(rail, "CALLS") if c.startswith("GET /workspaces")] == [
        "GET /workspaces?limit=200&offset=0", "GET /workspaces?limit=200&offset=200", "GET /workspaces?limit=200&offset=400"]
    assert js(rail, "MR.find('nv-rail-ws-note')") is None, "a whole list says nothing"


def test_the_rails_poll_is_one_page_per_15_s(rail):
    """Ruling 4: three pages read, so the real useResource schedules the 15 s poll at 45 s: one request per 15 s on average, as the one page it replaced."""
    rail.eval('MR.mount(NV_Rail, { selectedWorkspaceId: "w-0" });')
    settle(rail)
    polls = js(rail, "TIMERS.map(function (t) { return t.ms; })")
    assert 45000 in polls and 15000 not in polls


def test_the_rail_says_when_a_later_page_failed(rail):
    rail.eval('FAIL_FROM.workspaces = 400; MR.mount(NV_Rail, { selectedWorkspaceId: "w-0" });')
    settle(rail)
    assert len(js(rail, "ids('nv-rail-ws:')")) == 400
    assert rail.eval("textOf(MR.find('nv-rail-ws-note'))") == "Showing the first 400 workspaces; the rest did not load: the store is down"


def test_the_rail_counts_a_workspaces_sessions_from_its_row_and_lists_them_when_it_opens(rail):
    """w-1's 100 sessions are all older than the newest 200 of the install: counted in that window the rail said "0 sessions" and listed none."""
    rail.eval('MR.mount(NV_Rail, { selectedWorkspaceId: "w-0" });')
    settle(rail)
    assert rail.eval("textOf(MR.find('nv-rail-ws-count:w-1'))") == "100 sessions"
    assert rail.eval("textOf(MR.find('nv-rail-ws-count:w-0'))") == "250 sessions"
    assert rail.eval("textOf(MR.find('nv-rail-ws-count:w-2'))") == "0 sessions"
    rail.eval("MR.click('nv-rail-ws:w-1');")
    settle(rail)
    listed = js(rail, "ids('nv-rail-ws-session:')")
    assert listed == [f"s-{k}" for k in range(250, 350)]
    assert "POST /sessions/find w-1" in js(rail, "CALLS"), "the open workspace reads its own sessions, filtered on the server"
    assert js(rail, "MR.find('nv-rail-ws-more:w-1')") is None


def test_an_open_workspace_with_more_sessions_than_the_window_says_so(rail):
    rail.eval('MR.mount(NV_Rail, { selectedWorkspaceId: "w-0" });')
    settle(rail)
    rail.eval('MR.click("nv-rail-ws:w-0");')
    settle(rail)
    assert len(js(rail, "ids('nv-rail-ws-session:')")) == 200
    assert rail.eval("textOf(MR.find('nv-rail-ws-more:w-0'))") == "Showing the newest 200 of 250 sessions."


# ---- the palette ----------------------------------------------------------------------------------------------------------------------------------------


def _search(ctx, q: str) -> list[str]:
    ctx.eval("MR.mount(NV_Palette, {}); CON.paletteRef.current.open(); MR.rerender();")
    settle(ctx)
    ctx.eval(f"MR.find('nv-palette-input').props.onChange({{ target: {{ value: {json.dumps(q)} }} }});")
    settle(ctx)
    return js(ctx, "MR.findAll('nv-palette-row').map(function (e) { return e.props['data-row-key']; })")


def test_the_palette_finds_an_agent_past_the_200th(palette):
    assert "a:agent-449" in _search(palette, "agent-449")
    assert [c for c in js(palette, "CALLS") if c.startswith("GET /agents")] == [
        "GET /agents?limit=200&offset=0", "GET /agents?limit=200&offset=200", "GET /agents?limit=200&offset=400"]
    assert js(palette, "MR.findAll('nv-palette-note:')") == []


def test_the_palette_says_when_a_list_was_cut_short(palette):
    palette.eval("FAIL_FROM.agents = 200;")
    rows = _search(palette, "agent-")
    assert "a:agent-449" not in rows
    assert palette.eval("textOf(MR.find('nv-palette-note:agents'))") == "Showing the first 200 agents; the rest did not load: the store is down"
    assert js(palette, "MR.find('nv-palette-note:graphs')") is None


def test_the_palette_says_when_the_workspace_list_was_cut_short(palette):
    palette.eval("CON.workspacesList = { items: [{ id: 'w-0' }], truncated: true, serverTotal: 20000, error: null };")
    _search(palette, "x")
    assert palette.eval("textOf(MR.find('nv-palette-note:workspaces'))") == "Showing the first 1 of 20000 workspaces."


# ---- the phone's Spaces tab -----------------------------------------------------------------------------------------------------------------------------


def _rows(n: int) -> list[dict]:
    return [{"id": f"w-{i}", "name": f"Workspace {i}", "session_count": 250 if i == 0 else 100 if i == 1 else 0} for i in range(n)]


def test_the_spaces_tab_lists_every_workspace_and_notes_a_cut_short_list(spaces):
    """The Spaces tab draws con.workspaces (the shell's own list, every page) and, when that list was cut short, says so under it."""
    spaces.eval(f"CON.workspaces = {json.dumps(_rows(450))}; MR.mount(NV_MobileSpaces, {{}});")
    settle(spaces)
    shown = js(spaces, "ids('nv-mob-ws:')")
    assert len(shown) == 450 and shown[-1] == "w-449" and js(spaces, "MR.find('nv-mob-ws-note')") is None
    spaces.eval("CON.workspacesList = { items: CON.workspaces, truncated: true, serverTotal: null, error: null };")
    settle(spaces)
    assert spaces.eval("textOf(MR.find('nv-mob-ws-note'))") == "Showing the first 450 workspaces; there may be more."


def test_the_spaces_tab_counts_from_the_row_and_lists_an_open_workspaces_own_sessions(spaces):
    spaces.eval(f"CON.workspaces = {json.dumps(_rows(3))}; MR.mount(NV_MobileSpaces, {{}});")
    settle(spaces)
    assert spaces.eval("textOf(MR.find('nv-mob-ws-count:w-1'))") == "100 sessions"
    assert spaces.eval("textOf(MR.find('nv-mob-ws-count:w-2'))") == "0 sessions"
    spaces.eval("MR.click('nv-mob-ws:w-1');")
    settle(spaces)
    assert js(spaces, "ids('nv-mob-session:')") == [f"s-{k}" for k in range(250, 350)]
    spaces.eval("MR.click('nv-mob-ws:w-0');")
    settle(spaces)
    assert spaces.eval("textOf(MR.find('nv-mob-ws-more:w-0'))") == "Showing the newest 200 of 250 sessions."
