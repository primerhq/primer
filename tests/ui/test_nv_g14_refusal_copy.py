"""G14 fix B, part 2: G14-03 (LOW) the create-session bind picker's false empty, and G14-04 (LOW) the auth refusals that reach the user as raw machine codes.

G14-03: the bind picker (desktop overlay and phone sheet) builds its rows from the one-shot agents/graphs reads' data alone (``pollMs 0``) and its only empty branch
is ``!visible.length`` -> "No agents or graphs yet." - so a failed read draws the confident empty claim with no error line and no retry (reopening the overlay is the
only recovery). The "Default agent" row always stands and the no-binding submit path asks for the system default agent, so a session can still be created: the
mitigation that keeps this at LOW. The fix gates the empty claim on no read error: a failed read draws the refusal (the one reader's sentence, never the raw code)
with a Retry that refires the reads.

G14-04: the auth gate answers 401/403 with the bare code and nothing else (``detail={"error": "auth_required"}``; the problem handler's code fallback leaves the
wire ``detail`` the raw code), and ``readRefusal`` (ui/foundation/api.js, the one reader) already maps exactly those codes to sentences - but ``NV_errText``
(nv-overlays.jsx, drawn by the desktop create-session error line and the phone's Inbox and create-session error lines) and ``NV_failToast`` (nv-session-doc.jsx, the
Park/End/Delete failure toasts) pass ``err.detail || err.message`` straight through: the user reads a snake_case code where a sentence belongs. The fix routes both
helpers through ``readRefusal``.

Audit: /var/tmp/revamp/audit/G14.md; the auditor's probes (probe_g14_03_bind_picker_false_empty.py, probe_g14_04_raw_code_in_refusal_copy.py) pin the RED state,
these pin the GREEN state. Polarity: RED at the base commit, GREEN once the fix lands.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
OV = (UI / "components" / "console" / "nv-overlays.jsx").read_text(encoding="utf-8")
MOB = (UI / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")
SDOC = (UI / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")
API = (UI / "foundation" / "api.js").read_text(encoding="utf-8")
USE_RESOURCE = (UI / "foundation" / "use-resource.js").read_text(encoding="utf-8")
DEPS = (ROOT / "primer" / "api" / "deps.py").read_text(encoding="utf-8")
ERRORS = (ROOT / "primer" / "api" / "errors.py").read_text(encoding="utf-8")
NV_PLATFORM = (UI / "components" / "console" / "nv-platform.jsx").read_text(encoding="utf-8")
SH_API = UI / "components" / "shell" / "sh-api.jsx"

# The real wire shapes (tests/_support/refusal_envelopes.py answers the same through the real handlers): the auth gate sends only the
# code and the handler's code fallback leaves it in `detail`; readRefusal's bare-code table maps it to a sentence.
_GATE = {"status": 401, "title": "Unauthorized", "detail": "auth_required", "extensions": {"error": "auth_required"}}
_ROLE = {"status": 403, "title": "Forbidden", "detail": "forbidden_role", "extensions": {"error": "forbidden_role"}}
_CONFLICT = {"status": 409, "title": "Conflict", "detail": "Session 'sess-1' has ended"}
_NETWORK = {"type": "/errors/network-error", "title": "Network error", "status": 0, "detail": "Failed to fetch"}


# ---- G14-04: the two helpers route the refusal through the one reader ------------------------------------------------------


def test_the_wire_shape_and_the_mapper_are_what_the_fix_builds_on():
    assert 'detail={"error": "auth_required"}' in DEPS, "the auth gate's object-detail raise moved (re-verify the anchors)"
    assert 'for key in ("code", "error"):' in ERRORS, "the problem handler's code fallback moved (re-verify the anchors)"
    assert 'auth_required: "Your session has ended; sign in again."' in API, "readRefusal's bare-code table moved"
    assert 'forbidden_role: "Your role does not allow this."' in API


def _helper_body(src: str, name: str) -> str:
    """The source between ``function name`` and the next line-starting ``}``, as the audit probe slices it."""
    i = src.index("function " + name)
    return src[i:src.index("\n}", i)]


def _helper_source(src: str, name: str) -> str:
    """The complete function through its closing brace, for loading into V8."""
    return _helper_body(src, name) + "\n}"


def test_nv_errText_stops_passing_the_raw_code_through():
    body = _helper_body(OV, "NV_errText")
    assert "err.detail || err.message" not in body, (
        "G14-04: NV_errText still passes err.detail || err.message straight through - the user reads the machine code"
    )
    assert "readRefusal" in body, "NV_errText must say the refusal through the one reader, not a second copy of the code table"


def test_nv_failToast_stops_passing_the_raw_code_through():
    body = _helper_body(SDOC, "NV_failToast")
    assert "readRefusal" in body, (
        "G14-04: NV_failToast still toasts err.detail || err.message - 'Park failed: auth_required'"
    )
    # The one reader takes precedence over the raw fallback, which exists only
    # for contexts where the reader is not loaded (the house guarded shape,
    # as PR_PagedNote's).
    assert body.index("readRefusal") < body.index("err.detail || err.message"), (
        "the one reader must take precedence over the raw fallback"
    )


def test_the_helpers_are_still_wired_into_their_render_sites():
    assert 'data-testid="nv-ns-error"' in OV and "NV_errText(err)" in OV, "the desktop create-session error line moved"
    assert 'data-testid="nv-mob-ns-error"' in MOB and "NV_errText(err)" in MOB, "the phone create-session error line moved"
    assert "NV_failToast(toast, \"Park\", err)" in SDOC, "the Park failure toast moved"


# ---- G14-03: the bind picker's empty claim is gated on no read error --------------------------------------------------------


def test_the_fix_keeps_the_bind_picker_anchors():
    assert '"nv-ov:agents"' in OV and '"nv-ov:graphs"' in OV, "the create-session overlay's one-shot reads moved"
    assert 'data-testid="nv-ns-bind-menu"' in OV, "the desktop bind menu testid moved"
    assert 'data-testid="nv-mob-ns-bind-list"' in MOB, "the phone bind list testid moved"
    assert 'data-testid="nv-ns-bind-default"' in OV and 'data-testid="nv-mob-ns-bind-default"' in MOB, (
        "the 'Default agent' row moved: it is the mitigation that keeps this at LOW"
    )


def _desktop_menu() -> str:
    start = OV.index('data-testid="nv-ns-bind-menu"')
    return OV[start:OV.index('<NV_Field label="Name"', start)]


def _phone_bind_list() -> str:
    start = MOB.index('data-testid="nv-mob-ns-bind-list"')
    return MOB[start:MOB.index("</React.Fragment>", start)]


def test_the_desktop_bind_menu_consults_the_reads_error_state():
    menu = _desktop_menu()
    assert re.search(r"agents\.error \|\| graphs\.error \? \(", menu), (
        "G14-03: the bind menu never consults the agents/graphs reads' error state - a failed read claims 'No agents or graphs yet.'"
    )
    assert re.search(
        r"data-testid=\"nv-ns-bind-error\"[\s\S]*?NV_errText\(agents\.error \|\| graphs\.error\)[\s\S]*?data-testid=\"nv-ns-bind-retry\"",
        menu,
    ), "the error line must say the refusal through the one reader and offer a Retry that refires the reads"
    assert 'role="alert"' in menu, "the error line must announce"
    assert "agents.refetch()" in menu and "graphs.refetch()" in menu, "the Retry must refetch the failed reads"
    assert '["agent or graph", "agents or graphs"]' in menu, "the honest empty claim must survive for a truly empty list"


def test_the_phone_bind_stage_consults_the_reads_error_state():
    region = _phone_bind_list()
    assert re.search(r"agents\.error \|\| graphs\.error \? \(", region), (
        "G14-03: the phone's bind stage never consults the reads' error state - a failed read claims 'No agents or graphs yet.'"
    )
    assert re.search(
        r"data-testid=\"nv-mob-ns-bind-error\"[\s\S]*?NV_errText\(agents\.error \|\| graphs\.error\)[\s\S]*?data-testid=\"nv-mob-ns-bind-retry\"",
        region,
    )
    assert 'role="alert"' in region
    assert "agents.refetch()" in region and "graphs.refetch()" in region
    assert '["agent or graph", "agents or graphs"]' in region


# ---- executed: the helpers say the sentence, not the code -------------------------------------------------------------------


@pytest.fixture
def errtext():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(API)
    ctx.eval(_helper_source(OV, "NV_errText"))
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def failtoast():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis; var TOASTS = []; function toast(msg, extra) { TOASTS.push([msg, extra || null]); }")
    ctx.eval(API)
    ctx.eval(_helper_source(SDOC, "NV_failToast"))
    try:
        yield ctx
    finally:
        ctx.close()


def _toasts(ctx) -> list:
    return json.loads(ctx.eval("JSON.stringify(TOASTS)"))


def test_nv_errText_says_the_mapper_sentence_for_the_auth_gate(errtext):
    got = errtext.eval("NV_errText(new window.primerApi.ApiError(" + json.dumps(_GATE) + "))")
    assert got == "Your session has ended; sign in again.", f"G14-04: the user reads {got!r} where the sentence belongs"
    assert "auth_required" not in got


def test_nv_errText_says_the_mapper_sentence_for_a_role_refusal(errtext):
    got = errtext.eval("NV_errText(new window.primerApi.ApiError(" + json.dumps(_ROLE) + "))")
    assert got == "Your role does not allow this."
    assert "forbidden_role" not in got


def test_nv_errText_keeps_the_server_sentence_for_other_failures(errtext):
    """The mapper is not a rewrite: a refusal with the server's own sentence says it, and a network error says what the browser said."""
    conflict = errtext.eval("NV_errText(new window.primerApi.ApiError(" + json.dumps(_CONFLICT) + "))")
    assert conflict == "Session 'sess-1' has ended", conflict
    network = errtext.eval("NV_errText(new window.primerApi.ApiError(" + json.dumps(_NETWORK) + "))")
    assert network == "Failed to fetch", network
    assert errtext.eval("NV_errText(null)") is None


def test_nv_failToast_says_the_mapper_sentence_for_the_auth_gate(failtoast):
    failtoast.eval("NV_failToast(toast, 'Park', new window.primerApi.ApiError(" + json.dumps(_GATE) + "));")
    toasts = _toasts(failtoast)
    assert toasts == [["Park failed: Your session has ended; sign in again.", {"kind": "error", "requestId": None}]], (
        f"G14-04: the toast carries the machine code: {toasts!r}"
    )


def test_nv_failToast_keeps_the_server_sentence_and_the_request_id(failtoast):
    """The held behavior test_session_actions.py pins: the server's own reason and the request id, word for word."""
    failtoast.eval(
        "NV_failToast(toast, 'Delete', new window.primerApi.ApiError("
        + json.dumps(_CONFLICT | {"extensions": {"request_id": "req-409"}})
        + "));"
    )
    assert _toasts(failtoast) == [
        ["Delete failed: Session 'sess-1' has ended", {"kind": "error", "requestId": "req-409"}],
    ]


def test_nv_failToast_falls_back_to_the_error_message_when_the_server_said_nothing(failtoast):
    failtoast.eval("NV_failToast(toast, 'End', new Error('Failed to fetch'));")
    assert _toasts(failtoast) == [["End failed: Failed to fetch", {"kind": "error", "requestId": None}]]


# ---- executed: the bind pickers draw the refusal and a retry, not the false empty -------------------------------------------

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
var FormField = function (props) { return props.children; };
var CALLS = [];
var FAIL = { agents: null, graphs: null };
var EMPTY = false;
function apiFetchStub(method, path, body, opts) {
  CALLS.push(method + " " + path);
  if (path.indexOf("/agents") === 0) {
    if (FAIL.agents) return Promise.reject(new window.primerApi.ApiError(FAIL.agents));
    if (EMPTY) return Promise.resolve({ kind: "offset", offset: 0, length: 0, total: 0, items: [] });
    return Promise.resolve({ kind: "offset", offset: 0, length: 1, total: 1, items: [{ id: "agent-1", name: "agent-1", description: "a" }] });
  }
  if (path.indexOf("/graphs") === 0) {
    if (FAIL.graphs) return Promise.reject(new window.primerApi.ApiError(FAIL.graphs));
    return Promise.resolve({ kind: "offset", offset: 0, length: 0, total: 0, items: [] });
  }
  return Promise.resolve({ items: [] });
}
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

# The overlay's focus and escape hooks live in foundation scripts this test does not load (the menu never closes under test).
_OPS_STUBS = (
    "window.primerApi.useEscape = function () {};"
    " window.primerApi.useFocusTrap = function () {};"
    " window.primerApi.focusablesOf = function () { return []; };"
    " window.primerApi.apiFetch = apiFetchStub;"
)

# NV_errText is the one reader's sentence at the error sites - the real helper, as the code holds it when the test runs.
_ERRTEXT = _helper_source(OV, "NV_errText")
_EMPTYTEXT = NV_PLATFORM[NV_PLATFORM.index("function NV_emptyText("):NV_PLATFORM.index("// Per-entity page config.")]


@cache
def _code(*files: Path) -> str:
    return API + "\n" + USE_RESOURCE + "\n" + "\n".join(transpile(f) for f in files)


def _js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def _settle(ctx) -> None:
    """Draw what the reads answered: each eval ends with V8's microtask queue drained, so the reads' chains land between two evals."""
    for _ in range(6):
        ctx.eval("MR.rerender();")


@pytest.fixture
def desktop():
    c = mini_react_context(
        _code(SH_API, UI / "components" / "console" / "nv-overlays.jsx"),
        _PRELUDE
        + "\n" + _ERRTEXT + "\n" + _EMPTYTEXT
        + "\nvar CON = { wid: 'w-1', workspaces: [{ id: 'w-1', name: 'w-1' }], toast: function () {}, setDoc: function () {},"
        + " promoteDoc: function () {}, stampSessionMeta: function () {}, closeOverlay: function () {} };"
        + " function NV_useConsole() { return CON; }",
    )
    c.eval(_OPS_STUBS)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def mobile():
    c = mini_react_context(
        _code(SH_API, UI / "components" / "console" / "nv-mobile-shell.jsx"),
        _PRELUDE
        + "\n" + _ERRTEXT + "\n" + _EMPTYTEXT
        + "\nvar CON = { workspaces: [{ id: 'w-1', name: 'w-1' }] };"
        + " function NV_useConsole() { return CON; }"
        + " window.NV_sessionStateChipView = function () { return {}; }; window.Fab = function () { return null; };"
        + " window.BottomSheet = function (p) { return p.open ? React.createElement(React.Fragment, null, p.children) : null; };",
    )
    c.eval(_OPS_STUBS)
    try:
        yield c
    finally:
        c.close()


def test_the_desktop_bind_menu_says_the_error_and_offers_a_retry_when_the_reads_failed(desktop):
    """A one-shot read that failed yields zero rows: the menu must draw the refusal and a Retry, not the confident empty claim."""
    desktop.eval(
        "FAIL.agents = " + json.dumps(_GATE) + ";"
        " FAIL.graphs = " + json.dumps(_GATE) + ";"
        " MR.mount(NV_CreateSessionOverlay, {});"
    )
    _settle(desktop)
    desktop.eval("MR.click('nv-ns-bind');")
    _settle(desktop)
    err = desktop.eval("MR.find('nv-ns-bind-error')")
    assert err is not None, (
        "G14-03: a failed bind read claims 'No agents or graphs yet.' with no error line and no retry; the menu drew "
        + desktop.eval("textOf(MR.find('nv-ns-bind-menu'))")
    )
    text = desktop.eval("textOf(MR.find('nv-ns-bind-error'))")
    assert "Your session has ended; sign in again." in text, f"the error line must say the mapper's sentence, not the code: {text!r}"
    assert "auth_required" not in text, f"the raw machine code reaches the user: {text!r}"
    assert "No agents or graphs yet." not in " ".join(_js(desktop, "MR.texts()")), "the false empty claim must not stand with the read failed"
    assert desktop.eval("!!MR.find('nv-ns-bind-default')"), "the 'Default agent' row must survive: a session can still be created"
    # The Retry refires the reads, and a recovered read draws the rows again.
    desktop.eval("FAIL.agents = null; FAIL.graphs = null; MR.click('nv-ns-bind-retry');")
    _settle(desktop)
    assert desktop.eval("!!MR.find('nv-ns-bind:agent-1')"), "a recovered read must draw the rows"
    assert not desktop.eval("!!MR.find('nv-ns-bind-error')"), "a recovered read must clear the error state"


def test_the_desktop_bind_menu_still_says_empty_when_the_reads_succeeded_and_are_empty(desktop):
    """The held claim: an honest empty list (reads answered, nothing there) still says 'No agents or graphs yet.'"""
    desktop.eval("EMPTY = true; MR.mount(NV_CreateSessionOverlay, {});")
    _settle(desktop)
    desktop.eval("MR.click('nv-ns-bind');")
    _settle(desktop)
    assert "No agents or graphs yet." in " ".join(_js(desktop, "MR.texts()"))
    assert not desktop.eval("!!MR.find('nv-ns-bind-error')"), "a succeeded empty read is not an error state"


def test_the_phone_bind_stage_says_the_error_and_offers_a_retry_when_the_reads_failed(mobile):
    mobile.eval(
        "FAIL.agents = " + json.dumps(_GATE) + ";"
        " FAIL.graphs = " + json.dumps(_GATE) + ";"
        " MR.mount(NV_MobileCreateSessionSheet, { open: true, workspaces: [{ id: 'w-1', name: 'w-1' }], onClose: function () {} });"
    )
    _settle(mobile)
    mobile.eval("MR.click('nv-mob-ns-wid:w-1');")
    _settle(mobile)
    err = mobile.eval("MR.find('nv-mob-ns-bind-error')")
    assert err is not None, (
        "G14-03: the phone's bind stage claims 'No agents or graphs yet.' when the reads failed; the list drew "
        + mobile.eval("textOf(MR.find('nv-mob-ns-bind-list'))")
    )
    text = mobile.eval("textOf(MR.find('nv-mob-ns-bind-error'))")
    assert "Your session has ended; sign in again." in text, f"the error line must say the mapper's sentence, not the code: {text!r}"
    assert "auth_required" not in text
    assert "No agents or graphs yet." not in " ".join(_js(mobile, "MR.texts()"))
    mobile.eval("FAIL.agents = null; FAIL.graphs = null; MR.click('nv-mob-ns-bind-retry');")
    _settle(mobile)
    assert mobile.eval("!!MR.find('nv-mob-ns-bind:agent-1')"), "a recovered read must draw the rows"
    assert not mobile.eval("!!MR.find('nv-mob-ns-bind-error')"), "a recovered read must clear the error state"
