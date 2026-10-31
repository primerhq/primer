"""The console for per-person MCP sign-ins (access-control plan P5 Task 6).

A browser sign-in is the caller's own: ``TS_McpAuthPanel`` says "Your sign-in" and its Connect is enabled for whoever the status route answers
(the route answers only a person who may use the toolset), admin or not. A client-credentials sign-in is shared: the panel says "Shared sign-in"
and its buttons need full access on the toolset (``your_level`` of ``GET /toolsets/{id}/access`` is ``full`` or ``owner``), else it says who can
connect it. The disconnect copy names whose runs lose access. The catalogue card and the tool picker read the caller's own state from
``/v1/tools``. The Activity view renders a ``user:<id>`` actor as the username (admins load ``/admin/users``), anything else as it is.

The components run in MiniRacer on their real source (``tests/ui/_mini_react.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
AUTH = ROOT / "ui" / "components" / "toolsets" / "mcp-auth.jsx"
ACTIVITY = ROOT / "ui" / "components" / "shell" / "sh-activity.jsx"
SH_API = ROOT / "ui" / "components" / "shell" / "sh-api.jsx"
PICKER = ROOT / "ui" / "components" / "shared" / "tool-picker.jsx"

PANEL_PRELUDE = r"""
var STATUS = { toolset_id: "kb", mode: "oauth_browser", state: "needs_sign_in", reason: "Not connected.", connected_at: null, expires_at: null, per_user: true };
var ACCESS = null; var ACCESS_ERROR = null; var KEYS = []; var CALLS = []; var TOASTS = [];
function PK_Section(p) { return React.createElement("section", { "data-testid": p.testid }, p.children); }
function PK_Segmented() { return null; } function PK_Grid(p) { return React.createElement("div", null, p.children); }
function PK_Field(p) { return React.createElement("div", null, p.children); } function PK_Fold(p) { return React.createElement("div", null, p.children); }
function TS_KvEditor() { return null; }
function Btn(p) { return React.createElement("button", Object.assign({}, p), p.children); }
function Banner(p) { return React.createElement("div", { "data-testid": p.testid || "banner" }, p.title, p.detail, p.actions); }
window.confirmDialog = function (opts) { window.__confirmOpts = opts; return Promise.resolve(false); };
window.primerApi = {
  toastPush: function (t) { TOASTS.push(t); },
  useResource: function (key) {
    KEYS.push(key);
    if (key === null) return { data: undefined, loading: false, error: null, refetch: function () {} };
    if (String(key).indexOf("toolset-access:") === 0) return { data: ACCESS, loading: false, error: ACCESS_ERROR, refetch: function () {} };
    if (key === "auth-status") throw new Error("the panel no longer reads the caller's role");
    return { data: STATUS, loading: false, error: null, refetch: function () {} };
  },
  useMutation: function () { return { loading: false, error: null, mutate: function () { return Promise.resolve({}); } }; },
  apiFetch: function (method, path) { CALLS.push([method, path]); return Promise.resolve(STATUS); },
};
"""


@pytest.fixture
def panel():
    ctx = mini_react_context(transpile(AUTH), PANEL_PRELUDE)
    try:
        yield ctx
    finally:
        ctx.close()


def _mount(ctx) -> None:
    ctx.eval("MR.mount(TS_McpAuthPanel, {toolsetId:'kb', pushToast:function(t){TOASTS.push(t);}})")


def _texts(ctx) -> str:
    return " ".join(json.loads(ctx.eval("JSON.stringify(MR.texts())")))


def _cc(ctx, *, level: str | None, state: str = "connected") -> None:
    ctx.eval(
        "STATUS = {toolset_id:'kb', mode:'oauth_client_credentials', state:" + json.dumps(state) + ", connected_at:"
        + ("'now'" if state == "connected" else "null") + ", expires_at:null, per_user:false};"
        + "ACCESS = " + (json.dumps({"your_level": level}) if level else "null") + ";"
        + ("ACCESS_ERROR = {status:404, detail:'not found'};" if level is None else "")
    )


def test_non_admin_grantee_sees_an_enabled_connect_on_their_own_sign_in(panel) -> None:
    _mount(panel)

    assert panel.eval("MR.find('ts-auth-connect') !== null")
    assert panel.eval("!MR.find('ts-auth-connect').props.disabled"), "the status route answered, so the caller may connect"
    assert "Your sign-in" in _texts(panel)
    assert panel.eval("MR.find('ts-auth-admin-note') === null")
    assert "Only an admin" not in _texts(panel)
    assert panel.eval("KEYS.indexOf('auth-status')") == -1
    assert panel.eval("KEYS.filter(function (k) { return k !== null && String(k).indexOf('toolset-access:') === 0; }).length") == 0, (
        "a browser sign-in needs no access read"
    )


def test_your_sign_in_disconnect_copy(panel) -> None:
    panel.eval("STATUS = {toolset_id:'kb', mode:'oauth_browser', state:'connected', connected_at:'now', expires_at:null, per_user:true}")
    _mount(panel)
    assert panel.eval("!MR.find('ts-auth-disconnect').props.disabled")

    panel.eval("MR.click('ts-auth-disconnect'); MR.rerender()")

    assert panel.eval("window.__confirmOpts.message") == "Your runs lose access to this toolset until you connect again."


@pytest.mark.parametrize("level", [None, "read"])
def test_client_credentials_buttons_disabled_below_full(panel, level) -> None:
    _cc(panel, level=level)
    _mount(panel)

    assert "Shared sign-in" in _texts(panel)
    assert panel.eval("!!MR.find('ts-auth-reconnect').props.disabled")
    assert panel.eval("!!MR.find('ts-auth-disconnect').props.disabled")
    assert panel.eval("MR.find('ts-auth-full-note') !== null")
    assert "Only people with full access can connect this toolset" in _texts(panel)


@pytest.mark.parametrize("level", ["full", "owner"])
def test_client_credentials_buttons_enabled_with_full(panel, level) -> None:
    _cc(panel, level=level)
    _mount(panel)

    assert panel.eval("!MR.find('ts-auth-reconnect').props.disabled")
    assert panel.eval("!MR.find('ts-auth-disconnect').props.disabled")
    assert panel.eval("MR.find('ts-auth-full-note') === null")
    panel.eval("MR.click('ts-auth-disconnect'); MR.rerender()")
    assert panel.eval("window.__confirmOpts.message") == (
        "Every agent that uses this toolset loses access to it until someone with full access connects it again."
    )


def test_the_admin_note_is_gone_from_the_source() -> None:
    src = AUTH.read_text(encoding="utf-8")
    assert "ts-auth-admin-note" not in src
    assert "Only an admin can connect this toolset." not in src
    assert "until an admin connects it again" not in src


@pytest.mark.parametrize(
    ("auth", "label"),
    [
        ({"state": "needs_sign_in", "per_user": True}, "Needs your sign-in"),
        ({"state": "needs_sign_in", "per_user": False}, "Needs sign-in"),
        ({"state": "connected", "per_user": True}, "Connected"),
    ],
)
def test_the_card_reads_the_callers_own_state(panel, auth, label) -> None:
    got = json.loads(panel.eval("JSON.stringify(TS_cardAuthStatus(" + json.dumps({"id": "kb", "available": True, "auth": auth}) + "))"))
    assert got["label"] == label


def test_the_tool_picker_names_a_sign_in_the_caller_needs() -> None:
    src = PICKER.read_text(encoding="utf-8")
    assert "needs your sign-in" in src and "needs sign-in" in src
    assert "entry.auth" in src


# ---- Activity ----------------------------------------------------------------------------------------------------------------------------------


ACTIVITY_PRELUDE = r"""
var USERS = { items: [{ id: "u-ada", username: "ada" }] }; var USERS_FAIL = false;
function PR_PagedNote() { return null; }
window.PR_partialFailure = function () { return null; };
var EVENTS = { items: [
  { id: 1, event_type: "toolset.updated", entity_kind: "toolset", entity_id: "kb", actor: "user:u-ada", session_id: null, occurred_at: "2026-10-31T10:00:00Z" },
  { id: 2, event_type: "toolset.updated", entity_kind: "toolset", entity_id: "kb", actor: "user:u-gone", session_id: null, occurred_at: "2026-10-31T10:00:01Z" },
  { id: 3, event_type: "session.steered", entity_kind: null, entity_id: null, actor: "system", session_id: "s-1", occurred_at: "2026-10-31T10:00:02Z" },
], max_id: 3 };
function settled(v) { return { then: function (ok) { var r = ok(v); return settled(r); }, catch: function () { return settled(v); } }; }
function failing(e) { return { then: function (_ok, bad) { if (bad) bad(e); return failing(e); }, catch: function (bad) { bad(e); return settled(null); } }; }
var SH_api = {
  events: function () { return settled(EVENTS); },
  eventSubscriptions: function () { return settled({ items: [] }); },
  adminUsers: function () { return USERS_FAIL ? failing(new Error("forbidden")) : settled(USERS); },
};
window.SH_api = SH_api;
"""


def _activity():
    return mini_react_context(transpile(ACTIVITY), ACTIVITY_PRELUDE)


def test_activity_renders_a_user_actor_as_the_username() -> None:
    ctx = _activity()
    try:
        assert ctx.eval("SH_activityActor('user:u-ada', {'u-ada': 'ada'})") == "ada"
        assert ctx.eval("SH_activityActor('user:u-gone', {'u-ada': 'ada'})") == "user:u-gone", "an unknown user stays as it is"
        assert ctx.eval("SH_activityActor('system', {'u-ada': 'ada'})") == "system"
        assert ctx.eval("SH_activityActor('trigger:tr-1', {})") == "trigger:tr-1"
        assert ctx.eval("SH_activityActor(null, {})") == ""

        ctx.eval("MR.mount(SH_ActivityPanel, {}); MR.rerender(); MR.rerender()")
        texts = json.loads(ctx.eval("JSON.stringify(MR.texts())"))
        assert "ada" in texts and "user:u-gone" in texts and "system" in texts
        assert "user:u-ada" not in texts
    finally:
        ctx.close()


def test_activity_without_the_user_list_shows_the_raw_actor() -> None:
    """A caller who cannot read /admin/users (or a list that fails) still sees every event, with the actor as stored."""
    ctx = _activity()
    try:
        ctx.eval("USERS_FAIL = true; MR.mount(SH_ActivityPanel, {}); MR.rerender(); MR.rerender()")
        texts = json.loads(ctx.eval("JSON.stringify(MR.texts())"))
        assert "user:u-ada" in texts
    finally:
        ctx.close()


def test_sh_api_reads_every_page_of_admin_users() -> None:
    prelude = """
var __calls = [];
window.primerApi = { apiFetch: function (m, p) { __calls.push([m, p]); return null; } };
var PR_fetchAllPages = function (path, opts) { __calls.push(["pages", path, opts]); return null; };
"""
    ctx = mini_react_context(transpile(SH_API), prelude)
    try:
        ctx.eval("SH_api.adminUsers('SIG')")
        assert json.loads(ctx.eval("JSON.stringify(__calls)"))[-1] == ["pages", "/admin/users", {"signal": "SIG"}]
    finally:
        ctx.close()
