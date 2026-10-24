"""The console's first-load workspace is the default one (pivot P2 Task 14).

``GET /v1/workspaces`` flags the default workspace (prime) with a response-only ``is_default``. When the address names no workspace the shell
picks that row, else the first listed (an install with more workspaces than the console lists, or an older server that sends no flag), else none.
``NV_defaultWid`` is the pick; it is sliced out of ``nv-shell.jsx`` and run in V8.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._support.js_source import blank, close_of

ROOT = Path(__file__).resolve().parents[2]
SHELL = (ROOT / "ui" / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")


def _function(name: str) -> str:
    """The source of one top-level ``function <name>(...) {...}`` of nv-shell.jsx."""
    start = SHELL.index(f"function {name}(")
    code = blank(SHELL, strings=True, templates=True)
    body = code.index("{", code.index(")", start))
    end = close_of(code, body)
    assert end > 0, name
    return SHELL[start:end]


@pytest.fixture
def ev():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval(_function("NV_defaultWid"))
    try:
        yield lambda expr: json.loads(ctx.eval(f"JSON.stringify({{v: {expr}}})")).get("v")
    finally:
        ctx.close()


def test_the_flagged_workspace_wins(ev) -> None:
    assert ev('NV_defaultWid([{id: "a"}, {id: "prime", is_default: true}])') == "prime"


def test_without_a_flag_the_first_listed_is_picked(ev) -> None:
    assert ev('NV_defaultWid([{id: "a"}, {id: "b"}])') == "a"
    assert ev('NV_defaultWid([{id: "a", is_default: false}, {id: "b", is_default: "yes"}])') == "a", "only true is the flag"


def test_no_workspace_is_no_pick(ev) -> None:
    assert ev("NV_defaultWid([])") is None
    assert ev("NV_defaultWid(null)") is None


def test_the_pick_is_exported_and_used_by_the_default_effect() -> None:
    assert "window.NV_defaultWid = NV_defaultWid;" in SHELL
    effect = SHELL[SHELL.index("// Default workspace: the URL's, else the flagged one"):]
    effect = effect[:effect.index("}, [wid, wsItems.length]);")]
    assert "setWid(function (current) { return current || NV_defaultWid(wsItems); })" in effect
    assert "wsItems[0].id" not in effect, "the first listed is only the fallback inside NV_defaultWid"


# ---- the first load lands in the caller's main session ----------------------------------------------------------------------------------------------------
#
# The plan's lead ruling 3 (pivot P2): each user has their own main operator session in the default workspace, made on demand by
# POST /v1/workspaces/{id}/main_session (created as the caller, so it runs at the caller's rank; the same row on every later call). When the address
# named no workspace and no document, the shell has picked the default workspace and the tab groups are still empty once the per-user working set is
# restored, it asks for that session, and it opens it as a tab only when the answer says this call CREATED it (`created: true`): the user's genuine
# first login, never a later load (the S7 follow-up ruling). A refused call opens nothing and says nothing. NV_useMainSessionOpen is the hook; it is
# sliced out of nv-shell.jsx with the helpers it reads and run under the hook runtime of tests/ui/_mini_react.py, mounted by a one-line component, so
# each test drives it through the states the shell would hand it.

UI = ROOT / "ui"

_HOOK_PRELUDE = (
    (UI / "foundation" / "api.js").read_text(encoding="utf-8")
    + "\n"
    + (UI / "foundation" / "tab-group-model.js").read_text(encoding="utf-8")
    + r"""
var CALLS = [];
var TOASTS = [];
var STAMPS = [];
var PENDING = null;
var ROW = { id: "sess-prime-main-1", name: "main", workspace_id: "prime", binding: { kind: "agent", agent_id: "operator" }, status: "created" };
var REPLY = { ok: Object.assign({ created: true }, ROW) };
window.primerApi.apiFetch = function (method, path, body) {
  CALLS.push([method, path, body]);
  if (REPLY.pending) return new Promise(function (resolve, reject) { PENDING = { resolve: resolve, reject: reject }; });
  if (REPLY.err) return Promise.reject(REPLY.err);
  return Promise.resolve(REPLY.ok);
};
window.primerApi.toastPush = function (t) { TOASTS.push(t); };
var MODEL = TG_init({ groupId: "g1" });
var NAV = { current: false };
var ENV = {
  initialWid: null, initialDoc: null, ready: true, wid: "prime",
  wsItems: [{ id: "a" }, { id: "prime", name: "prime", is_default: true }],
  navigatedRef: NAV,
  setTgModel: function (fn) { MODEL = typeof fn === "function" ? fn(MODEL) : fn; },
  stampSessionMeta: function (sid, meta) { STAMPS.push([sid, meta]); },
};
function env(over) {
  var e = Object.assign({}, ENV, over || {});
  e.tgModel = MODEL;
  return e;
}
function Probe(p) { NV_useMainSessionOpen(p.env); return null; }
function tabIds() {
  var out = [];
  MODEL.groups.forEach(function (g) { g.tabs.forEach(function (t) { out.push(t.id + (t.preview ? " (preview)" : "")); }); });
  return out;
}
"""
)


@pytest.fixture
def hook():
    from tests.ui._mini_react import mini_react_context

    # NV_toastPayload rides along although the hook no longer toasts: a toast that crept back must reach TOASTS, not die on a ReferenceError.
    code = "\n".join(_function(n) for n in ("NV_toastPayload", "NV_flaggedWid", "NV_hasNoTab", "NV_useMainSessionOpen"))
    ctx = mini_react_context(code, _HOOK_PRELUDE)

    class Hook:
        def js(self, expr: str):
            return json.loads(ctx.eval(f"JSON.stringify({{v: {expr}}})")).get("v")

        def run(self, stmt: str) -> None:
            ctx.eval(stmt)
            ctx.eval("void 0")  # let the request's promise settle

        def mount(self, over: str = "{}") -> None:
            self.run(f"MR.mount(Probe, {{ env: env({over}) }})")

        def rerender(self, over: str = "{}") -> None:
            self.run(f"MR.rerender({{ env: env({over}) }})")

    try:
        yield Hook()
    finally:
        ctx.close()


def test_the_first_login_opens_the_main_session_the_call_created(hook) -> None:
    hook.mount()
    assert hook.js("CALLS") == [["POST", "/workspaces/prime/main_session", None]]
    assert hook.js("tabIds()") == ["session:sess-prime-main-1 (preview)"]
    assert hook.js("STAMPS") == [["sess-prime-main-1", {"wid": "prime", "name": "main", "binding": {"kind": "agent", "agent_id": "operator"}}]]
    assert hook.js("TOASTS") == []


@pytest.mark.parametrize("answer", ["Object.assign({ created: false }, ROW)", "ROW", "Object.assign({ created: 'yes' }, ROW)"])
def test_a_main_session_the_call_did_not_create_opens_nothing(hook, answer) -> None:
    """A later load (the user already has their main session), a server that says nothing, or anything but ``true``: no tab, no toast."""
    hook.run(f"REPLY = {{ ok: {answer} }}")
    hook.mount()
    hook.rerender()
    assert len(hook.js("CALLS")) == 1
    assert hook.js("tabIds()") == [] and hook.js("STAMPS") == [] and hook.js("TOASTS") == []


def test_it_asks_once_per_page_load(hook) -> None:
    hook.mount()
    hook.run("MODEL = TG_init({ groupId: 'g1' })")  # the person closed the tab
    for over in ("{}", '{ wid: "a" }', '{ wid: "prime" }', "{ ready: false }", "{ ready: true }"):
        hook.rerender(over)
    assert len(hook.js("CALLS")) == 1 and hook.js("tabIds()") == []


@pytest.mark.parametrize("over", ['{ initialWid: "prime" }', '{ initialDoc: { kind: "file", ref: "notes.md" } }'])
def test_a_link_that_named_a_workspace_or_a_document_opens_nothing(hook, over) -> None:
    hook.mount(over)
    hook.rerender(over)
    assert hook.js("CALLS") == [] and hook.js("tabIds()") == []


def test_it_waits_for_the_restore_and_the_pick(hook) -> None:
    hook.mount("{ ready: false }")
    hook.rerender("{ ready: true, wid: null }")
    assert hook.js("CALLS") == [], "no workspace picked yet"
    hook.rerender("{ ready: true, wid: 'prime' }")
    assert len(hook.js("CALLS")) == 1 and hook.js("tabIds()") == ["session:sess-prime-main-1 (preview)"]


def test_a_restored_working_set_wins(hook) -> None:
    hook.run('MODEL = TG_openTab(MODEL, { kind: "session", ref: "sess-yesterday" }, { promote: true })')
    hook.mount()
    hook.rerender()
    assert hook.js("CALLS") == [] and hook.js("tabIds()") == ["session:sess-yesterday"]


def test_another_workspace_selected_first_wins_for_good(hook) -> None:
    hook.mount('{ wid: "a" }')
    hook.rerender('{ wid: "prime" }')
    assert hook.js("CALLS") == []


def test_without_a_flagged_workspace_nothing_is_created(hook) -> None:
    """The route makes a session on the first call, so it is asked only of the real default workspace, never of the first-listed fallback."""
    hook.mount('{ wid: "a", wsItems: [{ id: "a" }, { id: "b" }] }')
    hook.rerender('{ wid: "a", wsItems: [{ id: "a" }, { id: "b" }] }')
    assert hook.js("CALLS") == []


def test_a_navigation_before_the_answer_drops_it(hook) -> None:
    hook.run("REPLY = { pending: true }")
    hook.mount()
    assert len(hook.js("CALLS")) == 1
    hook.run("NAV.current = true; PENDING.resolve(Object.assign({ created: true }, ROW))")
    hook.rerender()
    assert hook.js("tabIds()") == [] and hook.js("STAMPS") == []


def test_another_workspace_selected_before_the_answer_drops_it(hook) -> None:
    hook.run("REPLY = { pending: true }")
    hook.mount()
    hook.rerender('{ wid: "a" }')
    hook.run("PENDING.resolve(Object.assign({ created: true }, ROW))")
    assert hook.js("tabIds()") == []


def test_a_tab_opened_before_the_answer_is_kept(hook) -> None:
    hook.run("REPLY = { pending: true }")
    hook.mount()
    hook.run('MODEL = TG_openTab(MODEL, { kind: "file", ref: "notes.md" }, {})')
    hook.run("PENDING.resolve(Object.assign({ created: true }, ROW))")
    assert hook.js("tabIds()") == ["file:notes.md (preview)"]


def test_a_refused_call_opens_nothing_and_says_nothing(hook) -> None:
    """The open is a landing nicety, not something the person asked for: a refusal leaves the console as it is, with no toast."""
    hook.run(
        "REPLY = { err: { status: 404, detail: \"Agent 'operator' does not exist\", requestId: 'req-main', "
        "envelope: { detail: \"Agent 'operator' does not exist\", extensions: { code: 'not_found' } } } }"
    )
    hook.mount()
    hook.rerender()
    assert len(hook.js("CALLS")) == 1
    assert hook.js("TOASTS") == [] and hook.js("tabIds()") == [] and hook.js("STAMPS") == []


def test_the_shell_wires_the_hook_without_a_history_entry() -> None:
    hook_src = _function("NV_useMainSessionOpen")
    assert "markPush" not in hook_src and "pushState" not in hook_src, "the open is landing state: the URL write replaces"
    shell = SHELL[SHELL.index("function NV_Shell()"):]
    assert "NV_useMainSessionOpen({" in shell
    call = shell[shell.index("NV_useMainSessionOpen({"):]
    call = call[:call.index("});")]
    for part in ("initialWid: initial.wid", "initialDoc: initial.doc", "navigatedRef: navigatedRef", "tgModel: tgModel"):
        assert part in call, part
    # Every navigation marks the load as touched: a verb (markPush) and a link, Back or Forward (the listener, past its own-write check).
    mark = shell[shell.index("var markPush = React.useCallback("):]
    assert "navigatedRef.current = true;" in mark[:mark.index("}, []);")]
    listener = shell[shell.index("function onNav() {"):shell.index("readUrlRef.current = onNav;")]
    assert listener.index("if (current === ownHashRef.current) return;") < listener.index("navigatedRef.current = true;")
