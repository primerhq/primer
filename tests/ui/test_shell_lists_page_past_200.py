"""The shell's lists read every page (ticket 01a16dfa): the rail's workspaces, the palette's entities and the phone's Spaces tab.

After #911 removed the paged Workspaces page, a workspace past the first 200 was reachable nowhere in the console: the rail, the phone's Spaces tab and
the palette all read ``SH_api.workspaces``, which asked for one page of 200. The rail and the palette are run here on the hook runtime with the REAL
``ui/foundation/api.js`` (``PR_fetchAllPages``, ``PR_usePagedResource``, ``PR_PagedNote``) and the REAL ``ui/components/shell/sh-api.jsx``, against a fake
``apiFetch`` that pages the way the server does (``limit`` and ``offset``, a ``total``) and a ``useResource`` that runs each fetcher once and keeps its
answer and its options.
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
SH_API = UI / "components" / "shell" / "sh-api.jsx"
RAIL = UI / "components" / "console" / "nv-rail.jsx"
PALETTE = UI / "components" / "console" / "nv-palette.jsx"
MOBILE = UI / "components" / "console" / "nv-mobile-shell.jsx"

_PRELUDE = r"""
var window = globalThis;
window.innerWidth = 1280; window.innerHeight = 800;
var document = { addEventListener: function () {}, removeEventListener: function () {}, body: { style: {} } };
window.addEventListener = function () {}; window.removeEventListener = function () {};
function NV_identity() { return { d: "", color: "" }; }
window.useWorkspaceTapListener = function () {};
var CON = { paletteRef: { current: {} }, wid: "w-0", registry: [], doc: null, frecency: null, workspaces: [], workspacesList: null };
function NV_useConsole() { return CON; }
function SH_rankVerbs() { return []; }
"""

# The server: COUNTS[name] rows of each list, `<prefix>-<n>` in id order, FAIL_FROM[name] the first offset that answers 500 (a page that fails mid-way).
# useResource runs the fetcher of a key and its deps once (the first render that asks) and answers with what it resolved to; OPTS[key] is the latest options
# the key was given.
_SERVER = r"""
var COUNTS = { workspaces: 450, agents: 450, graphs: 3, toolsets: 0 };
var PREFIX = { workspaces: "w", agents: "agent", graphs: "graph", toolsets: "ts" };
var FAIL_FROM = {};
var CALLS = [];
window.primerApi.apiFetch = function (method, path, body) {
  CALLS.push(method + " " + path);
  var m = /^\/(workspaces|agents|graphs|toolsets)\?limit=(\d+)&offset=(\d+)$/.exec(path);
  if (m) {
    var name = m[1], limit = Number(m[2]), offset = Number(m[3]);
    if (FAIL_FROM[name] != null && offset >= FAIL_FROM[name]) {
      return Promise.reject(new window.primerApi.ApiError({ status: 500, title: "Internal Server Error", detail: "the store is down" }));
    }
    var n = Math.max(0, Math.min(limit, COUNTS[name] - offset)), items = [];
    for (var i = 0; i < n; i++) items.push({ id: PREFIX[name] + "-" + (offset + i), name: "Workspace " + (offset + i) });
    return Promise.resolve({ kind: "offset", offset: offset, length: n, total: COUNTS[name], items: items });
  }
  return Promise.resolve({ items: [] });
};
var RES = {}, OPTS = {};
window.primerApi.useResource = function (key, fetcher, opts) {
  OPTS[key] = opts || {};
  var k = key + "::" + JSON.stringify((opts && opts.deps) || []);
  var e = RES[k];
  if (!e) {
    e = RES[k] = { data: undefined };
    fetcher(null).then(function (d) { e.data = d; }, function (err) { e.error = err; });
  }
  return { data: e.data, error: e.error || null, loading: e.data === undefined, degraded: false, refetch: function () {} };
};
window.primerApi.useEscape = function () {};
window.primerApi.useFocusTrap = function () {};
function ids(prefix) { return MR.findAll(prefix).map(function (e) { return e.props["data-testid"].slice(prefix.length); }); }
"""


@cache
def _code(*files: Path) -> str:
    return API + "\n" + "\n".join(transpile(f) for f in files)


def _ctx(*files: Path):
    ctx = mini_react_context(_code(*files), _PRELUDE)
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


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def settle(ctx) -> None:
    """Draw what the reads answered: each eval ends with V8's microtask queue drained, so a paged read's chain of pages lands between two evals, and
    the render after it draws the rows (and the effect that lengthens the poll runs)."""
    for _ in range(3):
        ctx.eval("MR.rerender();")


def test_the_rail_lists_every_workspace_past_200(rail):
    rail.eval('MR.mount(NV_Rail, { selectedWorkspaceId: "w-449" });')
    settle(rail)
    shown = js(rail, "ids('nv-rail-ws:')")
    assert len(shown) == 450 and shown[0] == "w-0" and shown[-1] == "w-449"
    assert [c for c in js(rail, "CALLS") if c.startswith("GET /workspaces")] == [
        "GET /workspaces?limit=200&offset=0", "GET /workspaces?limit=200&offset=200", "GET /workspaces?limit=200&offset=400"]
    assert js(rail, "MR.find('nv-rail-ws-note')") is None, "a whole list says nothing"


def test_the_rails_poll_is_one_page_per_15_s(rail):
    """Ruling 4: three pages read, so the 15 s poll becomes 45 s: one request per 15 s on average, as the one page it replaced."""
    rail.eval('MR.mount(NV_Rail, { selectedWorkspaceId: "w-0" });')
    assert js(rail, "OPTS['nv-workspaces'].pollMs") == 15000
    settle(rail)
    assert js(rail, "OPTS['nv-workspaces'].pollMs") == 45000


def test_the_rail_says_when_a_later_page_failed(rail):
    rail.eval('FAIL_FROM.workspaces = 400; MR.mount(NV_Rail, { selectedWorkspaceId: "w-0" });')
    settle(rail)
    assert len(js(rail, "ids('nv-rail-ws:')")) == 400
    assert js(rail, "MR.find('nv-rail-ws-note').props.children") == (
        "Showing the first 400 workspaces; the rest did not load: the store is down")


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
    assert js(palette, "MR.find('nv-palette-note:agents').props.children") == (
        "Showing the first 200 agents; the rest did not load: the store is down")
    assert js(palette, "MR.find('nv-palette-note:graphs')") is None


def test_the_palette_says_when_the_workspace_list_was_cut_short(palette):
    palette.eval("CON.workspacesList = { items: [{ id: 'w-0' }], truncated: true, serverTotal: 20000, error: null };")
    _search(palette, "x")
    assert js(palette, "MR.find('nv-palette-note:workspaces').props.children") == "Showing the first 1 of 20000 workspaces."


# ---- the phone's Spaces tab ---------------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def spaces():
    c = mini_react_context(_code(SH_API, MOBILE), _PRELUDE + "window.NV_sessionStateChipView = function () { return {}; };"
                           " window.Fab = function () { return null; }; window.BottomSheet = function () { return null; };")
    c.eval(_SERVER)
    try:
        yield c
    finally:
        c.close()


def test_the_spaces_tab_lists_every_workspace_and_notes_a_cut_short_list(spaces):
    """The Spaces tab draws con.workspaces (the shell's own list, every page) and, when that list was cut short, says so under it."""
    rows = [{"id": f"w-{i}", "name": f"Workspace {i}"} for i in range(450)]
    spaces.eval(f"CON.workspaces = {json.dumps(rows)}; MR.mount(NV_MobileSpaces, {{}});")
    settle(spaces)
    shown = js(spaces, "ids('nv-mob-ws:')")
    assert len(shown) == 450 and shown[-1] == "w-449" and js(spaces, "MR.find('nv-mob-ws-note')") is None
    spaces.eval("CON.workspacesList = { items: CON.workspaces, truncated: true, serverTotal: null, error: null };")
    settle(spaces)
    assert js(spaces, "MR.find('nv-mob-ws-note').props.children") == "Showing the first 450 workspaces; there may be more."
