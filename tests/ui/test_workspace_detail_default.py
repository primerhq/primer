"""The workspace settings overlay never offers to destroy the default workspace (pivot P2 Task 13).

``WorkspaceDetail`` (``ui/components/workspaces.jsx``) reads the workspace's row (``GET /v1/workspaces/{id}``), which carries the response-only
``is_default``. For the default workspace (prime) the Destroy tab is not offered, and a link that names it (``?tab=destroy``, the overlay's section
slot) shows a note saying why instead of ``WS_DestroyTab``: the server refuses the delete (403 ``reserved_workspace``). Until the row has been read
the console cannot tell, so neither the tab nor the destroy panel is shown. Any other workspace keeps both.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
WORKSPACES = ROOT / "ui" / "components" / "workspaces.jsx"

_PRELUDE = r"""
var window = globalThis;
var ROWS = {};
var QUERY = { tab: "destroy" };
var MOBILE = false;
var LOG = { deletes: [], navigated: [] };
window.primerApi = {
  useResource: function (key) {
    if (key.indexOf("workspace-detail:") === 0) {
      var wid = key.slice("workspace-detail:".length);
      return wid in ROWS ? { data: ROWS[wid], loading: false, error: null } : { data: null, loading: true, error: null };
    }
    return { data: { items: [] }, loading: false, error: null };
  },
  useRouter: function () { return { params: {}, query: QUERY, navigate: function (p) { LOG.navigated.push(p); } }; },
  useViewport: function () { return { isMobile: MOBILE }; },
  useMutation: function (fn) { return { loading: false, mutate: function () { LOG.deletes.push(1); return fn(); } }; },
  apiFetch: function () { return Promise.resolve({}); },
};
window.WorkspaceTap = function () { return null; };
function Icon() { return null; }
function Btn(p) { return React.createElement("button", { "data-testid": p["data-testid"], onClick: p.onClick, disabled: p.disabled }, p.children); }
function Banner(p) { return React.createElement("div", { className: "banner banner-" + p.kind }, p.title, p.detail ? React.createElement("span", null, p.detail) : null); }
function Modal(p) { return React.createElement("div", { "data-testid": "modal" }, p.children, p.footer); }
function MobileTabs(p) {
  return React.createElement("div", { "data-testid": "mobile-tabs" }, p.tabs.map(function (t) {
    return React.createElement("span", { key: t.id, "data-testid": "mobile-tab:" + t.id }, t.label);
  }));
}
function StatusPill() { return null; }
function relativeTime(s) { return String(s); }
function fmtDate(s) { return String(s); }
"""


@pytest.fixture(scope="module")
def code() -> str:
    return transpile(WORKSPACES)


@pytest.fixture
def ctx(code):
    c = mini_react_context(code, _PRELUDE)
    try:
        yield c
    finally:
        c.close()


def _tabs(ctx) -> list[str]:
    return json.loads(ctx.eval(
        'JSON.stringify(MR.findAll("workspace-tab:").map(function (e) { return e.props["data-testid"].slice("workspace-tab:".length); }))'
    ))


def _text(ctx) -> str:
    return "".join(json.loads(ctx.eval("JSON.stringify(MR.texts())")))


def _mount(ctx, wid: str) -> None:
    ctx.eval(f"MR.mount(WorkspaceDetail, {{ workspaceId: {json.dumps(wid)}, pushToast: function () {{}} }})")


def test_a_destroy_link_on_prime_shows_the_note(ctx) -> None:
    ctx.eval('ROWS.prime = { id: "prime", name: "prime", is_default: true }')
    _mount(ctx, "prime")
    assert ctx.eval('!!MR.find("ws-destroy-reserved")') is True
    text = _text(ctx)
    assert "This is the default workspace" in text and "prime cannot be deleted." in text
    assert "Destroy workspace" not in text and "Permanent action" not in text


def test_an_adopted_default_is_named_by_its_display_name(ctx) -> None:
    """An install that predates prime keeps the id ``primer`` and shows the name ``prime`` (m009 renames it)."""
    ctx.eval('ROWS.primer = { id: "primer", name: "prime", is_default: true }')
    _mount(ctx, "primer")
    assert "prime cannot be deleted." in _text(ctx)


def test_prime_offers_no_destroy_tab(ctx) -> None:
    ctx.eval('ROWS.prime = { id: "prime", name: "prime", is_default: true }; QUERY.tab = "files"')
    _mount(ctx, "prime")
    assert _tabs(ctx) == ["files", "sessions", "events", "log", "channels", "config"]


def test_another_workspace_keeps_its_destroy_tab_and_panel(ctx) -> None:
    ctx.eval('ROWS["ws-1"] = { id: "ws-1", name: "scratch", is_default: false }')
    _mount(ctx, "ws-1")
    assert _tabs(ctx) == ["files", "sessions", "events", "log", "channels", "config", "destroy"]
    assert ctx.eval('MR.find("ws-destroy-reserved") === null')
    text = _text(ctx)
    assert "Permanent action" in text and "Destroy workspace" in text


def test_a_row_without_the_flag_keeps_destroy(ctx) -> None:
    """An older server sends no ``is_default``: the server is still the guard, so the console keeps the tab."""
    ctx.eval('ROWS["ws-2"] = { id: "ws-2" }')
    _mount(ctx, "ws-2")
    assert "destroy" in _tabs(ctx) and "Destroy workspace" in _text(ctx)


def test_nothing_destructive_shows_before_the_row_is_read(ctx) -> None:
    """The flag arrives with the row: until then the console cannot tell prime from any other workspace."""
    _mount(ctx, "prime")
    assert "destroy" not in _tabs(ctx)
    text = _text(ctx)
    assert "Destroy workspace" not in text and ctx.eval('MR.find("ws-destroy-reserved") === null')
    ctx.eval('ROWS.prime = { id: "prime", name: "prime", is_default: true }; MR.rerender()')
    assert ctx.eval('!!MR.find("ws-destroy-reserved")') is True


def test_the_phone_layout_never_had_a_destroy_tab(ctx) -> None:
    ctx.eval('ROWS["ws-1"] = { id: "ws-1", is_default: false }; MOBILE = true; QUERY.tab = "files"')
    _mount(ctx, "ws-1")
    tabs = json.loads(ctx.eval('JSON.stringify(MR.findAll("mobile-tab:").map(function (e) { return e.props["data-testid"]; }))'))
    assert tabs == ["mobile-tab:files", "mobile-tab:sessions", "mobile-tab:logs", "mobile-tab:config"]
