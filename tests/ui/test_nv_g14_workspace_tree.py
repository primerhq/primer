"""G14 fix B, part 1: G14-02 (MEDIUM) - a failed first read of the workspace list is indistinguishable from an empty fleet.

The shell's single "nv-workspaces" read (PR_usePagedResource, 15 s poll) records the failure, but the console context exposes only
``workspacesList: workspaces.data || null`` - the resource's error is dropped before it reaches any surface. The desktop rail maps the
list with no empty or error branch (a silent empty box); the phone draws the confident "No workspaces yet." on the Spaces tab, the
create-session sheet's workspace stage and the files-tab picker, whenever ``!workspaces.length``. Partial (refresh) failures DO speak
(PR_pagedNote's list.error branch, stale-while-error keeps rows) - the gap is exactly the first-read / sustained-failure case.

Audit: /var/tmp/revamp/audit/G14.md; the auditor's probe (probe_g14_02_workspace_tree_first_read_silent.py) pins the RED state, these
pin the GREEN state. Polarity: RED at the base commit (the error never reaches a tree surface), GREEN once the fix lands: the context
exposes the read's error, the rail and the three phone sites draw the refusal (the one reader's sentence, never the raw code) with a
Retry that refires the read, and a cut-short read keeps its rows and its note.
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
SHELL = (UI / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")
RAIL = (UI / "components" / "console" / "nv-rail.jsx").read_text(encoding="utf-8")
MOBILE = (UI / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")
OVERLAYS = (UI / "components" / "console" / "nv-overlays.jsx").read_text(encoding="utf-8")
API = (UI / "foundation" / "api.js").read_text(encoding="utf-8")
USE_RESOURCE = (UI / "foundation" / "use-resource.js").read_text(encoding="utf-8")
SH_API = UI / "components" / "shell" / "sh-api.jsx"
NV_PLATFORM = (UI / "components" / "console" / "nv-platform.jsx").read_text(encoding="utf-8")

OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while OPEN_CONTEXTS:
        OPEN_CONTEXTS.pop().close()


# ---- the anchors the fix must keep (re-verify the G14-02 lines when they move) ---------------------------------------------


def test_the_fix_keeps_the_workspace_tree_anchors():
    assert '"nv-workspaces"' in SHELL, "the nv-workspaces resource key moved"
    assert "workspacesList: workspaces.data || null" in SHELL, "the nv-shell workspaces context line moved"
    assert 'testid="nv-rail-ws-note"' in RAIL, "the rail workspace note testid moved"
    assert 'data-testid="nv-mob-ws-tree"' in MOBILE, "the mobile Spaces tree testid moved"
    assert "No workspaces yet." in MOBILE, "the mobile empty text moved"


# ---- G14-02: the read's error reaches the tree surfaces -----------------------------------------------------------------------


def test_the_context_exposes_the_workspaces_read_error():
    """The root cause: the context hands the surfaces the rows only (workspaces.data || null) and drops the resource's error."""
    m = re.search(r"workspacesError:\s*workspaces\.error\s*\|\|\s*null", SHELL)
    assert m, "G14-02: the console context still drops the nv-workspaces read's error (only the rows reach the tree surfaces)"
    # The same read, not a second one: the error must come off the shell's own "nv-workspaces"
    # resource - the `workspaces` variable the context reads off is that read's.
    assert re.search(r'var workspaces = PR_usePagedResource\(\s*"nv-workspaces"', SHELL), ("the shell's workspaces read moved")


def test_the_rail_draws_an_error_state_with_a_retry_when_the_read_failed():
    """A failed first read (error present, no rows) is a silent empty box on the desktop rail today."""
    m = re.search(
        r"\{wsRes\.error && !workspaces\.length \? \(.*?data-testid=\"nv-rail-ws-error\".*?\) : null\}", RAIL, re.S
    )
    assert m, "G14-02: a failed first read of the workspace list is a silent empty box on the desktop rail"
    block = m.group(0)
    assert "NV_errText(wsRes.error)" in block, "the rail's error state must say the refusal through the one reader (NV_errText), not a raw code"
    assert "wsRes.refetch()" in block, "the rail's error state must offer a Retry that refires the read"
    assert 'role="alert"' in block, "the rail's error state must announce"


def test_the_phone_spaces_tab_never_claims_empty_when_the_read_failed():
    m = re.search(r'data-testid="nv-mob-ws-tree"[\s\S]*?No workspaces yet\.', MOBILE)
    assert m, "the Spaces tree block moved"
    region = m.group(0)
    assert re.search(r"con\.workspacesError \?[\s\S]*?data-testid=\"nv-mob-ws-error\"", region), (
        "G14-02: the phone Spaces tab claims 'No workspaces yet.' when the read failed"
    )
    assert "NV_errText(con.workspacesError)" in region, "the Spaces error state must say the refusal through the one reader"
    assert "con.refetchWorkspaces" in region and "nv-mob-ws-retry" in region, "the Spaces error state must offer a Retry that refires the read"
    assert 'role="alert"' in region


def test_the_create_session_sheet_workspace_stage_never_claims_empty_when_the_read_failed():
    m = re.search(r'data-testid="nv-mob-ns-workspace-list"[\s\S]*?No workspaces yet\.', MOBILE)
    assert m, "the create-session sheet's workspace stage moved"
    region = m.group(0)
    assert re.search(r"props\.workspacesError \?[\s\S]*?data-testid=\"nv-mob-ns-workspaces-error\"", region), (
        "G14-02: the create-session sheet's workspace stage claims 'No workspaces yet.' when the read failed"
    )
    assert "NV_errText(props.workspacesError)" in region
    assert "props.refetchWorkspaces" in region and "nv-mob-ns-workspaces-retry" in region
    # The stage is a leaf: the Spaces panel must hand it the read's error and the refire.
    # The tag's onCreated prop holds a multi-line function body, so the tag
    # outgrows any fixed window: match to the tag's own closing slash.
    tag = re.search(r"<NV_MobileCreateSessionSheet[\s\S]*?/>", MOBILE)
    assert tag, "the create-session sheet mount moved"
    assert "workspacesError={con.workspacesError}" in tag.group(0), "the sheet must receive the read's error from the console context"
    assert "refetchWorkspaces={con.refetchWorkspaces}" in tag.group(0), "the sheet must receive the refire from the console context"


def test_the_files_picker_never_claims_empty_when_the_read_failed():
    m = re.search(r'data-testid="nv-mob-files-ws-list"[\s\S]*?No workspaces yet\.', MOBILE)
    assert m, "the files-tab workspace picker moved"
    region = m.group(0)
    assert re.search(r"con\.workspacesError \?[\s\S]*?data-testid=\"nv-mob-files-ws-error\"", region), (
        "G14-02: the files-tab picker claims 'No workspaces yet.' when the read failed"
    )
    assert "NV_errText(con.workspacesError)" in region
    assert "con.refetchWorkspaces" in region and "nv-mob-files-ws-retry" in region


# ---- the held behaviors must survive the fix ----------------------------------------------------------------------------------


def test_a_partial_failure_note_still_speaks_on_the_rail():
    """A cut-short (refresh) read keeps its rows and its 'the rest did not load' note: the error state is for no-rows reads only."""
    note = re.search(r"<PR_PagedNote list=\{wsRes\.data\} noun=\"workspaces\" testid=\"nv-rail-ws-note\" />", RAIL)
    assert note, "the rail's paged note (the partial-failure voice) moved"
    m = re.search(r"\{wsRes\.error && !workspaces\.length \? \(", RAIL)
    assert m, "the rail's error state must be gated on a failed read with no rows to show"


# ---- executed: the rail and the phone draw the refusal, not the silent box ----------------------------------------------------

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
var CALLS = [];
var FAIL = { workspaces: null, agents: null, graphs: null };
var FAIL_FROM = null;
var COUNT = 450;
function apiFetchStub(method, path, body, opts) {
  CALLS.push(method + " " + path);
  if (path.indexOf("/workspaces") === 0) {
    var m = /^\/workspaces\?limit=(\d+)&offset=(\d+)$/.exec(path);
    if (!m) return Promise.resolve({ items: [] });
    var off = Number(m[2]);
    if (FAIL.workspaces && (FAIL_FROM == null || off >= FAIL_FROM)) {
      return Promise.reject(new window.primerApi.ApiError(FAIL.workspaces));
    }
    var n = Math.max(0, Math.min(Number(m[1]), COUNT - off)), items = [];
    for (var i = 0; i < n; i++) items.push({ id: "w-" + (off + i), name: "Workspace " + (off + i), session_count: 0 });
    return Promise.resolve({ kind: "offset", offset: off, length: n, total: COUNT, items: items });
  }
  if (path.indexOf("/agents") === 0) {
    if (FAIL.agents) return Promise.reject(new window.primerApi.ApiError(FAIL.agents));
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

# api.js extends (not replaces) the window.primerApi namespace, so a stub set in
# the prelude is overwritten by the real apiFetch when the code loads: install
# the working stub after the context, the same order test_nv_g14_refusal_copy.py
# uses for its overlay fixtures.
_PRIMER_STUBS = (
    "window.primerApi.useEscape = function () {};"
    " window.primerApi.useFocusTrap = function () {};"
    " window.primerApi.apiFetch = apiFetchStub;"
)

# NV_errText is the one reader's sentence at these sites - the real helper, as the fix writes it (G14-04 routes it through readRefusal).
_ERRTEXT = OVERLAYS[OVERLAYS.index("function NV_errText("):OVERLAYS.index("\n}\n", OVERLAYS.index("function NV_errText(")) + len("\n}\n")]

_EMPTYTEXT = NV_PLATFORM[NV_PLATFORM.index("function NV_emptyText("):NV_PLATFORM.index("// Per-entity page config.")]


def _auth_err():
    return {"status": 401, "title": "Unauthorized", "detail": "auth_required"}


@cache
def _code(*files: Path) -> str:
    return API + "\n" + USE_RESOURCE + "\n" + "\n".join(transpile(f) for f in files)


def _js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def _settle(ctx) -> None:
    """Draw what the reads answered: each eval ends with V8's microtask queue drained, so a paged read's chain lands between two evals."""
    for _ in range(6):
        ctx.eval("MR.rerender();")


@pytest.fixture
def rail():
    # NV_Rail reads the viewer's id through the console context (ACL-P2-T10's group split),
    # so the stand-in answers the hook with an empty context (no viewer: every row is Public).
    c = mini_react_context(
        _code(SH_API, UI / "components" / "console" / "nv-rail.jsx"),
        _PRELUDE + "\n" + _ERRTEXT + "\nvar CON = {};" + " function NV_useConsole() { return CON; }",
    )
    c.eval(_PRIMER_STUBS)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def spaces():
    # nv-rail.jsx loads first, as ui/index.html does: the Spaces tab's ownership
    # groups (ACL-P6-T3) come from its NV_wsGroupOf.
    c = mini_react_context(
        _code(SH_API, UI / "components" / "console" / "nv-rail.jsx", UI / "components" / "console" / "nv-mobile-shell.jsx"),
        _PRELUDE
        + "\n" + _ERRTEXT + "\n" + _EMPTYTEXT
        + "\nvar RETRIES = 0;"
        + " var CON = { workspaces: [], workspacesList: null, workspacesError: null, refetchWorkspaces: function () { RETRIES += 1; } };"
        + " function NV_useConsole() { return CON; }"
        + " window.NV_sessionStateChipView = function () { return {}; }; window.Fab = function () { return null; };"
        + " window.BottomSheet = function (p) { return p.open ? React.createElement(React.Fragment, null, p.children) : null; };",
    )
    c.eval(_PRIMER_STUBS)
    try:
        yield c
    finally:
        c.close()


def test_the_rail_draws_the_refusal_and_a_retry_when_the_first_read_fails(rail):
    """A first-page failure rejects as the request did: no rows, the error recorded on the resource."""
    rail.eval("FAIL.workspaces = " + json.dumps(_auth_err()) + "; MR.mount(NV_Rail, { selectedWorkspaceId: null });")
    _settle(rail)
    err = rail.eval("MR.find('nv-rail-ws-error')")
    assert err is not None, "G14-02: a failed first read of the workspace list is a silent empty box on the desktop rail"
    text = rail.eval("textOf(MR.find('nv-rail-ws-error'))")
    assert "Your session has ended; sign in again." in text, (
        f"the rail's error state must say the mapper's sentence, not the code: {text!r}"
    )
    assert "auth_required" not in text, f"the raw machine code reaches the user: {text!r}"
    # The Retry refires the read, and a recovered read draws the rows again.
    rail.eval("FAIL.workspaces = null; MR.click('nv-rail-ws-retry');")
    _settle(rail)
    assert _js(rail, "CALLS.filter(function (c) { return c.indexOf('GET /workspaces') === 0; }).length") >= 2, "the Retry must refire the read"
    assert rail.eval("MR.find('nv-rail-ws:w-0')") is not None, "a recovered read must draw the rows"
    assert rail.eval("MR.find('nv-rail-ws-error')") is None, "a recovered read must clear the error state"


def test_the_rail_keeps_its_rows_and_note_when_a_later_page_fails(rail):
    """Partial (refresh) failure: a page after the first fails, the rows read stay, and the note speaks - no error screen."""
    rail.eval(
        "FAIL.workspaces = { status: 500, title: 'Internal Server Error', detail: 'the store is down' };"
        " FAIL_FROM = 400; MR.mount(NV_Rail, { selectedWorkspaceId: null });"
    )
    _settle(rail)
    assert _js(rail, "MR.findAll('nv-rail-ws:').length") == 400, "the rows already read must stay"
    assert rail.eval("MR.find('nv-rail-ws-error')") is None, "a cut-short read with rows must not draw the error screen"
    assert rail.eval("textOf(MR.find('nv-rail-ws-note'))") == (
        "Showing the first 400 workspaces; the rest did not load: the store is down"
    ), "the partial-failure note keeps speaking"


def test_the_phone_spaces_tab_draws_the_refusal_and_a_retry_when_the_read_failed(spaces):
    spaces.eval("CON.workspacesError = new window.primerApi.ApiError(" + json.dumps(_auth_err()) + "); MR.mount(NV_MobileSpaces, {});")
    _settle(spaces)
    assert spaces.eval("!!MR.find('nv-mob-ws-error')"), (
        "G14-02: the phone Spaces tab claims 'No workspaces yet.' when the read failed"
    )
    text = spaces.eval("textOf(MR.find('nv-mob-ws-error'))")
    assert "Your session has ended; sign in again." in text, f"the error state must say the mapper's sentence: {text!r}"
    assert "auth_required" not in text
    all_text = " ".join(_js(spaces, "MR.texts()"))
    assert "No workspaces yet." not in all_text, "the confident empty claim must not stand with the read failed"
    spaces.eval("MR.click('nv-mob-ws-retry');")
    assert spaces.eval("RETRIES") == 1, "the Retry must refire the shell's read"


def test_the_phone_spaces_tab_keeps_its_rows_when_a_refresh_failed(spaces):
    """Stale-while-error: rows + error is a refresh that came back cut short - the rows stay, no error screen, no empty claim."""
    spaces.eval("CON.workspaces = [{ id: 'w-1', name: 'w-1', session_count: 0 }];")
    spaces.eval("CON.workspacesError = new window.primerApi.ApiError(" + json.dumps(_auth_err()) + "); MR.mount(NV_MobileSpaces, {});")
    _settle(spaces)
    assert spaces.eval("MR.find('nv-mob-ws:w-1')") is not None
    assert not spaces.eval("!!MR.find('nv-mob-ws-error')"), "rows + a failed refresh is not an error screen"
    assert "No workspaces yet." not in " ".join(_js(spaces, "MR.texts()"))
