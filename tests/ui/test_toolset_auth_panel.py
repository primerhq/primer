"""The MCP toolset overlay owns the shared sign-in lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
AUTH = ROOT / "ui" / "components" / "toolsets" / "mcp-auth.jsx"
TOOLSETS = ROOT / "ui" / "components" / "toolsets.jsx"

PRELUDE = r"""
var STATUS = { toolset_id: "kb", mode: "oauth_browser", state: "needs_sign_in", reason: "Not connected.", connected_at: null, expires_at: null };
var ROLE = "admin"; var CALLS = []; var TOASTS = []; var OPENED = []; var ASSIGNED = []; var CONFIRM = true; var TIMERS = [];
function PK_Section(p) { return React.createElement("section", { "data-testid": p.testid }, p.children); }
function PK_Segmented() { return null; } function PK_Grid(p) { return React.createElement("div", null, p.children); }
function PK_Field(p) { return React.createElement("div", null, p.children); } function PK_Fold(p) { return React.createElement("div", null, p.children); }
function TS_KvEditor() { return null; }
function Btn(p) { return React.createElement("button", Object.assign({}, p), p.children); }
function Banner(p) { return React.createElement("div", { "data-testid": p.testid || "banner" }, p.title, p.detail, p.actions); }
window.open = function (url) { OPENED.push(url); return window.__popupBlocked ? null : {}; };
window.location = { assign: function (url) { ASSIGNED.push(url); } };
window.confirmDialog = function (opts) { window.__confirmOpts = opts; return Promise.resolve(CONFIRM); };
globalThis.setTimeout = function (fn, ms) { TIMERS.push({ fn: fn, ms: ms }); return TIMERS.length; };
globalThis.clearTimeout = function () {};
window.primerApi = {
  toastPush: function (t) { TOASTS.push(t); },
  useViewport: function () { return { isMobile: !!window.__mobile }; },
  useResource: function (key) {
    if (key === "auth-status") return { data: { role: ROLE }, loading: false, error: null, refetch: function () {} };
    return { data: STATUS, loading: !!window.__loading, error: window.__statusError || null,
      refetch: function () { window.__refetches = (window.__refetches || 0) + 1; } };
  },
  useMutation: function (fetcher, opts) { return { loading: false, error: null, mutate: function (arg) {
    return Promise.resolve(fetcher(arg)).then(function (data) {
      (opts.invalidates || []).forEach(function (key) { window.__invalidated = (window.__invalidated || []).concat([key]); });
      if (opts.onSuccess) opts.onSuccess(data); return data;
    }, function (err) { if (opts.onError) opts.onError(err); throw err; });
  } }; },
  apiFetch: function (method, path) {
    CALLS.push([method, path]);
    if (window.__failure) return Promise.reject(window.__failure);
    if (path.endsWith("/oauth/start")) return Promise.resolve(window.__start || { authorize_url: "https://id.example/authorize" });
    return Promise.resolve(STATUS);
  },
};
"""


@pytest.fixture
def panel():
    ctx = mini_react_context(transpile(AUTH), PRELUDE)
    try:
        yield ctx
    finally:
        ctx.close()


def _mount(ctx, *, compact: bool = False) -> None:
    ctx.eval(
        "MR.mount(TS_McpAuthPanel, {toolsetId:'kb', compact:"
        + str(compact).lower()
        + ", pushToast:function(t){TOASTS.push(t);}})"
    )


def _texts(ctx) -> str:
    return " ".join(json.loads(ctx.eval("JSON.stringify(MR.texts())")))


@pytest.mark.parametrize(
    ("status", "text"),
    [
        ({"state": "connected", "connected_at": "now", "expires_at": "1970-01-01T00:52:00Z"}, "Connected expires in 52 min"),
        ({"state": "needs_sign_in", "reason": "Not connected."}, "Needs sign-in Not connected."),
        ({"state": "error", "reason": "Refresh failed."}, "Error Refresh failed."),
    ],
)
def test_status_lines(panel, status: dict, text: str) -> None:
    if status["state"] == "connected":
        panel.eval("Date.now = function () { return 0; }")
    panel.eval("STATUS = Object.assign({toolset_id:'kb',mode:'oauth_browser'}, " + json.dumps(status) + ")")
    _mount(panel)
    assert text in _texts(panel)
    assert panel.eval("MR.find('ts-auth-status') !== null")


def test_not_applicable_renders_nothing(panel) -> None:
    panel.eval("STATUS = {toolset_id:'kb',mode:'none',state:'not_applicable',reason:null}")
    _mount(panel)
    assert panel.eval("MR.find('ts-auth-panel') === null")


def test_admin_buttons_follow_the_state(panel) -> None:
    _mount(panel)
    assert panel.eval("MR.find('ts-auth-connect') !== null")
    assert panel.eval("MR.find('ts-auth-reconnect') === null")
    panel.eval("STATUS = {toolset_id:'kb',mode:'oauth_browser',state:'connected',connected_at:'now',expires_at:null}; MR.rerender()")
    assert panel.eval("MR.find('ts-auth-reconnect') !== null")
    assert panel.eval("MR.find('ts-auth-disconnect') !== null")


def test_a_non_admin_sees_no_enabled_buttons(panel) -> None:
    panel.eval("ROLE = 'user'")
    _mount(panel)
    assert panel.eval("!!MR.find('ts-auth-connect').props.disabled")
    assert "Only an admin can connect this toolset." in _texts(panel)


def test_connect_opens_the_sign_in_and_polls(panel) -> None:
    _mount(panel)
    panel.eval("MR.click('ts-auth-connect'); MR.rerender()")
    assert json.loads(panel.eval("JSON.stringify(OPENED)")) == ["https://id.example/authorize"]
    assert panel.eval("TIMERS.length > 0")
    panel.eval("STATUS = {toolset_id:'kb',mode:'oauth_browser',state:'connected',connected_at:'now',expires_at:null}; TIMERS.shift().fn(); MR.rerender()")
    assert panel.eval("TOASTS.filter(function(t){return t.title === 'Connected';}).length") == 1


def test_a_blocked_popup_falls_back_to_this_tab(panel) -> None:
    panel.eval("window.__popupBlocked = true")
    _mount(panel)
    panel.eval("MR.click('ts-auth-connect'); MR.rerender()")
    assert json.loads(panel.eval("JSON.stringify(ASSIGNED)")) == ["https://id.example/authorize"]


def test_client_credentials_connect_toasts_at_once(panel) -> None:
    panel.eval("window.__start = {authorize_url:null,status:{state:'connected',connected_at:'now'}}")
    _mount(panel)
    panel.eval("MR.click('ts-auth-connect'); MR.rerender()")
    assert panel.eval("TOASTS.filter(function(t){return t.title === 'Connected';}).length") == 1
    panel.eval("MR.click('ts-auth-connect'); MR.rerender()")
    assert panel.eval("TOASTS.filter(function(t){return t.title === 'Connected';}).length") == 2


def test_disconnect_asks_first(panel) -> None:
    panel.eval("STATUS = {toolset_id:'kb',mode:'oauth_browser',state:'connected',connected_at:'now',expires_at:null}; CONFIRM = false")
    _mount(panel)
    panel.eval("MR.click('ts-auth-disconnect'); MR.rerender()")
    assert panel.eval("window.__confirmOpts.title") == "Disconnect kb?"
    assert panel.eval("CALLS.filter(function(c){return c[1].endsWith('/oauth/disconnect');}).length") == 0


def test_a_failed_connect_shows_the_reason(panel) -> None:
    panel.eval("window.__failure = {detail:'Provider refused the client.'}")
    _mount(panel)
    panel.eval("MR.click('ts-auth-connect'); MR.rerender(); MR.rerender()")
    panel.eval("MR.rerender()")
    assert "Provider refused the client." in _texts(panel)


def test_take_oauth_return(panel) -> None:
    got = json.loads(
        panel.eval(
            "JSON.stringify(TS_takeOAuthReturn("
            "{pathname:'/console/',search:'?mcp_oauth=error&mcp_oauth_toolset=kb&mcp_oauth_error=invalid_state&x=1',hash:'#/?view=platform:toolsets&overlay=toolsets::kb'},"
            "{replaceState:function(a,b,url){window.__replaced=url;}}))"
        )
    )
    assert got == {"toolsetId": "kb", "outcome": "error", "code": "invalid_state"}
    assert panel.eval("window.__replaced") == "/console/?x=1#/?view=platform:toolsets&overlay=toolsets::kb"
    assert panel.eval("TS_OAUTH_RETURN_TEXT.invalid_state") == "The sign-in expired or was already used. Connect again."
    assert panel.eval("TS_oauthReturnText('something_new')") == "The sign-in did not complete. Connect again."


def test_the_fake_relink_button_is_gone() -> None:
    src = TOOLSETS.read_text(encoding="utf-8")
    assert "toolset-oauth-relink" not in src
    assert "console-relink-probe" not in src


def test_the_tools_and_post_save_results_explain_a_sign_in() -> None:
    src = TOOLSETS.read_text(encoding="utf-8")
    assert "This server needs a sign-in before its tools can be listed." in src
    assert 'title="Needs sign-in"' in src
    assert src.count("<TS_McpAuthPanel") >= 3


def test_buttons_stack_on_a_phone(panel) -> None:
    panel.eval("window.__mobile = true")
    _mount(panel)
    assert panel.eval("MR.find('ts-auth-actions').props['data-layout']") == "stacked"
