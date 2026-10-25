"""The desktop canvas panel (pivot P5, Task 20): ``NV_CanvasPanel`` / ``NV_CanvasFrame`` in
``ui/components/console/nv-canvas.jsx``, run in MiniRacer on the ``mini_react_context`` harness.

The pure seams are the bridge contract (``ui/canvas/renderer.js``'s header) and the lead's S8
rulings: the frame is sandboxed to an opaque origin (never same-origin), ``run_html`` is true ONLY
for the session's creator with ``strict_mode`` off (inert HTML, ruling 1), and the console's window
``message`` listener drops everything from the canvas frame tree after the port is up (ruling 2) -
before it, only the renderer's own ``ready`` reply from the renderer frame's window is accepted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
NV_CANVAS = ROOT / "ui" / "components" / "console" / "nv-canvas.jsx"
NV_SESSION_DOC = ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx"
SHELL_VERBS = ROOT / "ui" / "foundation" / "shell-verbs.js"
# The canvas helpers read a refusal through the foundation's one reader (window.primerApi.readRefusal), so the context loads the real api.js (it merges into the prelude's primerApi).
API_JS = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")

PLAYLIST_VIEW = json.dumps(
    {
        "session_id": "s1", "workspace_id": "w1", "rev": 2, "event_seq": 5,
        "element_count": 2,
        "elements": {"root": {"type": "Column", "children": ["a", "b"]}},
        "values": {}, "value_set_by": {}, "events": [],
        "settings": {"media_policy": "any-https", "media_allowlist": [], "strict_mode": False},
        "file_urls": {}, "frame_url": "/canvas/frame?v=1",
    }
)

EMPTY_VIEW = json.dumps(
    {
        "session_id": "s1", "workspace_id": "w1", "rev": 1, "event_seq": 0,
        "element_count": 0, "elements": {"root": {"type": "Column", "children": []}},
        "values": {}, "value_set_by": {}, "events": [],
        "settings": {"media_policy": "any-https", "media_allowlist": [], "strict_mode": False},
        "file_urls": {}, "frame_url": "/canvas/frame?v=1",
    }
)

SESSION = json.dumps({"id": "s1", "status": "running",
                      "initiated_by": {"type": "user", "id": "u1", "display": "Ada", "role": "user", "source": "local"}})

PRELUDE = r"""
var window = globalThis;
window.__resources = {};
window.__con = {
  wid: "w1", doc: { kind: "session", ref: "s1" }, canvas: "open",
  setCanvas: function (s) { window.__setCanvas = s; },
  viewer: { type: "user", id: "u1", display: "Ada", role: "user", source: "local" },
  toast: function (msg, extra) { (window.__toasts = window.__toasts || []).push([msg, extra]); },
};
window.NV_useConsole = function () { return window.__con; };
window.primerApi = {
  useResource: function (key, fetcher, opts) { return window.__resources[key] || { data: undefined, error: null, loading: true, degraded: false, refetch: function () {} }; },
  useViewport: function () { return { isMobile: window.__mobile || false, width: 1200 }; },
  toastPush: function (t) { (window.__pushed = window.__pushed || []).push(t); },
};
window.__storeChannels = {};
window.useSessionStore = function (wid, sid, channel) { return window.__storeChannels[channel] || null; };
window.SH_api = {
  keys: { canvas: function (sid) { return "nv-canvas:" + sid; } },
  canvas: function (wid, sid, signal) { return Promise.resolve({}); },
  canvasClear: function (wid, sid) { window.__cleared = [wid, sid]; return Promise.resolve({}); },
  canvasEvent: function (wid, sid, body) { (window.__events = window.__events || []).push(body); return Promise.resolve({}); },
};
window.confirmDialog = function (opts) { window.__lastConfirm = opts; return Promise.resolve(window.__confirmOk !== false); };
window.__ls = {};
window.localStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(window.__ls, k) ? window.__ls[k] : null; },
  setItem: function (k, v) { window.__ls[k] = String(v); },
  removeItem: function (k) { delete window.__ls[k]; },
};
window.crypto = { randomUUID: function () { window.__uuids = (window.__uuids || 0) + 1; return "uuid-" + window.__uuids; } };
window.open = function (url, target, features) { (window.__opened = window.__opened || []).push({ url: url, target: target, features: features }); return {}; };
window.location = { origin: "http://console.test", href: "http://console.test/console/" };
"""


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


@pytest.fixture
def ctx():
    py_mini_racer = pytest.importorskip("py_mini_racer")
    # the verb registry comes from the real shell-verbs.js (a plain JS file, no JSX),
    # so the verb labels/surfaces/availability are the real lint + ranking gates.
    code = transpile(NV_CANVAS) + "\n" + SHELL_VERBS.read_text(encoding="utf-8")
    c = mini_react_context(code, PRELUDE)
    # the real foundation reader (readRefusal): the canvas helpers read a refusal through it.
    c.eval(API_JS)
    try:
        yield c
    finally:
        c.close()


# --- visibility -----------------------------------------------------------------


@pytest.mark.parametrize("url, local, count, mobile, expected", [
    (None, None, 0, False, "hidden"),
    (None, None, 3, False, "shown"),
    ("closed", None, 3, False, "hidden"),
    ("open", None, 0, False, "shown"),
    (None, None, 3, True, "hidden"),
    (None, "open", 0, False, "shown"),
])
def test_visibility(ctx, url, local, count, mobile, expected) -> None:
    got = js(ctx, "NV_canvasVisibility(" + json.dumps({
        "urlState": url, "localState": local, "elementCount": count, "mobile": mobile,
    }) + ")")
    assert got == expected


# --- the frame ------------------------------------------------------------------


def test_the_frame_is_sandboxed_without_same_origin(ctx) -> None:
    """The iframe is sandboxed to an opaque origin: allow-scripts + allow-forms, no
    same-origin, autoplay allowed, no referrer, src is the /canvas/frame route (the CSP
    carrier), never the raw console copy."""
    ctx.eval("window.__resources['nv-canvas:s1'] = " + PLAYLIST_VIEW + ";")
    ctx.eval("MR.mount(window.NV_CanvasFrame, {sid: 's1', wid: 'w1', session: " + SESSION + ", data: " + PLAYLIST_VIEW + ", onEvent: function () {}});")
    frame = js(ctx, 'MR.find("nv-canvas-frame").props')
    assert (frame["sandbox"], frame["allow"], frame["referrerPolicy"]) == (
        "allow-scripts allow-forms", "autoplay", "no-referrer")
    assert frame["src"] == "/canvas/frame?v=1"


def test_no_frame_ever_gets_allow_same_origin() -> None:          # Review Focus 4 / ruling 4
    src = NV_CANVAS.read_text(encoding="utf-8")
    assert "allow-same-origin" not in src
    # the sandbox is the literal the renderer's own pin expects (test_canvas_static_guards)
    assert 'sandbox="allow-scripts allow-forms"' in src


# --- states ---------------------------------------------------------------------


def _mount_panel(ctx, resource_expr, *, canvas="open") -> None:
    ctx.eval("window.__con.canvas = " + json.dumps(canvas) + ";")
    ctx.eval("window.__resources['nv-canvas:s1'] = " + resource_expr + ";")
    ctx.eval("MR.mount(window.NV_CanvasPanel, {sid: 's1', wid: 'w1', session: " + SESSION + ", focused: true});")


def test_states_loading(ctx) -> None:
    _mount_panel(ctx, "{data: undefined, error: null, loading: true, degraded: false, refetch: function(){}}")
    assert js(ctx, '!!MR.find("nv-canvas-loading")')
    assert any("Loading canvas" in t for t in js(ctx, "MR.texts()"))


def test_states_error_has_try_again(ctx) -> None:
    err = json.dumps({"status": 500, "detail": "boom",
                      "envelope": {"status": 500, "detail": "boom", "extensions": {}}, "message": "boom"})
    _mount_panel(ctx, "{data: undefined, error: " + err + ", loading: false, degraded: false, refetch: function(){ window.__refetched = true; }}")
    assert js(ctx, '!!MR.find("nv-canvas-error")')
    assert any("The canvas could not load" in t for t in js(ctx, "MR.texts()"))
    assert js(ctx, '!!MR.find("nv-canvas-error-retry")')
    ctx.eval('MR.click("nv-canvas-error-retry")')
    assert js(ctx, "window.__refetched") is True


def test_states_empty_only_when_opened(ctx) -> None:
    _mount_panel(ctx, EMPTY_VIEW)
    assert js(ctx, '!!MR.find("nv-canvas-empty")')
    assert any("Nothing on the canvas yet" in t for t in js(ctx, "MR.texts()"))
    # an auto-hidden empty canvas (no explicit open, no elements) shows no empty state
    ctx.eval("MR.mount(window.NV_CanvasPanel, {sid: 's1', wid: 'w1', session: " + SESSION + ", focused: false});")
    ctx.eval("window.__con.canvas = null; window.__resources['nv-canvas:s1'] = " + EMPTY_VIEW + ";")
    ctx.eval("MR.rerender({sid: 's1', wid: 'w1', session: " + SESSION + ", focused: false});")
    assert js(ctx, 'MR.find("nv-canvas-empty")') is None


# --- split ----------------------------------------------------------------------


def test_the_split_is_clamped_and_stored(ctx) -> None:
    assert js(ctx, "NV_canvasClampSplit(10)") == 20
    assert js(ctx, "NV_canvasClampSplit(90)") == 80
    assert js(ctx, "NV_canvasClampSplit(50)") == 50
    assert js(ctx, "NV_canvasClampSplit(NaN)") == 50
    # default is 50 when nothing is stored
    assert js(ctx, "NV_canvasSplitGet()") == 50
    # set + get round-trip through localStorage
    assert js(ctx, "NV_canvasSplitSet(65)") == 65
    assert js(ctx, "window.__ls['primer.canvas.split']") == "65"
    assert js(ctx, "NV_canvasSplitGet()") == 65
    # an out-of-range stored value is clamped back into 20..80
    ctx.eval("window.__ls['primer.canvas.split'] = '99';")
    assert js(ctx, "NV_canvasSplitGet()") == 80


# --- frame message validation ---------------------------------------------------


def test_frame_messages_are_validated(ctx) -> None:
    # a well-formed event validates to its normalized shape
    ok = js(ctx, "NV_canvasValidateFrameMessage({type: 'event', element: 'a', kind: 'click'})")
    assert ok == {"type": "event", "element": "a", "kind": "click"}
    # an oversized message (over the 16 KiB frame->console cap) is dropped
    big = "NV_canvasValidateFrameMessage({type: 'event', element: 'a', kind: 'emit', data: '" + ("x" * (16 * 1024)) + "'})"
    assert js(ctx, big) is None
    # an unknown type is dropped
    assert js(ctx, "NV_canvasValidateFrameMessage({type: 'state', rev: 1})") is None
    # a bad element id (empty / too long / wrong case) is dropped
    assert js(ctx, "NV_canvasValidateFrameMessage({type: 'event', element: '', kind: 'click'})") is None
    assert js(ctx, "NV_canvasValidateFrameMessage({type: 'event', element: 'A', kind: 'click'})") is None
    long_id = "a" * 33
    assert js(ctx, "NV_canvasValidateFrameMessage({type: 'event', element: '" + long_id + "', kind: 'click'})") is None
    # a bad kind is dropped
    assert js(ctx, "NV_canvasValidateFrameMessage({type: 'event', element: 'a', kind: 'nonsense'})") is None


# --- event error text -----------------------------------------------------------


def _api_error(ctx, status, code, message) -> str:
    envelope = json.dumps({"status": status, "detail": message, "extensions": {"code": code}})
    return ("{status: " + str(status) + ", detail: " + json.dumps(message)
            + ", message: " + json.dumps(message) + ", envelope: " + envelope + "}")


def test_event_errors_read_well(ctx) -> None:
    e409 = _api_error(ctx, 409, "session_ended", "This session has ended, so the canvas no longer takes clicks.")
    assert js(ctx, "NV_canvasEventErrorText(" + e409 + ")") == "This session has ended, so the canvas no longer takes clicks."
    e429 = _api_error(ctx, 429, "rate_limited", "Too many waking events.")
    assert js(ctx, "NV_canvasEventErrorText(" + e429 + ")") == "Too many waking events."
    e422 = _api_error(ctx, 422, "unknown_element", "no such element")
    assert js(ctx, "NV_canvasEventErrorText(" + e422 + ")") == "That control is gone: the canvas changed."
    e422b = _api_error(ctx, 422, "kind_not_allowed", "clicks are not allowed here.")
    assert js(ctx, "NV_canvasEventErrorText(" + e422b + ")") == "Couldn't send that to the agent: clicks are not allowed here."


# --- verbs ----------------------------------------------------------------------


def test_the_verbs_register_with_pointer_affordances(ctx) -> None:
    ctx.eval("var __reg = SH_createVerbRegistry();")
    ctx.eval("window.NV_registerCanvasVerbs(__reg, function () { return {sid: 's1', wid: 'w1', con: window.__con, refetchAll: function () {}}; });")
    show = js(ctx, '__reg.get("canvas.show")')
    hide = js(ctx, '__reg.get("canvas.hide")')
    clear = js(ctx, '__reg.get("canvas.clear")')
    assert show["label"] == "Show Canvas"
    assert hide["label"] == "Hide Canvas"
    assert clear["label"] == "Clear Canvas"
    assert clear["destructive"] is True
    # each has a pointer surface (the tab-menu) - nothing is palette-only
    for v in (show, hide, clear):
        assert "tab-menu" in v["surfaces"]
    # availability tracks the panel's view: hidden -> show offered, hide not
    assert js(ctx, '__reg.get("canvas.show").available({session: {id: "s1"}})') is True
    assert js(ctx, '__reg.get("canvas.hide").available({session: {id: "s1"}})') is False
    assert js(ctx, '__reg.get("canvas.clear").available({session: {id: "s1"}})') is False
    # a shown panel with elements: hide + clear offered, show not
    ctx.eval("window.NV_CANVAS_VIEWS['s1'] = {shown: true, count: 2};")
    assert js(ctx, '__reg.get("canvas.show").available({session: {id: "s1"}})') is False
    assert js(ctx, '__reg.get("canvas.hide").available({session: {id: "s1"}})') is True
    assert js(ctx, '__reg.get("canvas.clear").available({session: {id: "s1"}})') is True
    # show/hide drive the URL canvas state (a replace, not a push)
    ctx.eval('__reg.get("canvas.show").run();')
    assert js(ctx, "window.__setCanvas") == "open"
    ctx.eval('__reg.get("canvas.hide").run();')
    assert js(ctx, "window.__setCanvas") == "closed"


def test_the_overflow_menu_has_the_canvas_rows() -> None:
    """The session overflow menu (nv-session-doc.jsx) carries the pointer affordances:
    Show canvas / Hide canvas / Clear canvas with their data-verb + testids."""
    src = NV_SESSION_DOC.read_text(encoding="utf-8")
    assert 'data-verb="canvas.show"' in src
    assert 'data-verb="canvas.hide"' in src
    assert 'data-verb="canvas.clear"' in src
    assert 'data-testid="nv-canvas-menu-show"' in src
    assert 'data-testid="nv-canvas-menu-hide"' in src
    assert 'data-testid="nv-canvas-menu-clear"' in src


# --- ruling 1: inert HTML (run_html) --------------------------------------------


@pytest.mark.parametrize("viewer_id, viewer_role, creator_id, strict, expected", [
    ("u1", "user", "u1", False, True),      # the creator, strict off -> runs at once
    ("u1", "user", "u1", True, False),      # the creator, strict on -> inert (strict wins)
    ("u2", "user", "u1", False, False),     # a different user -> inert
    ("u1", "admin", "u1", False, True),     # the creator (admin role is irrelevant to identity)
    ("u9", "admin", "u1", False, False),    # an admin who did NOT create it -> inert
])
def test_run_html_is_the_creators_only(ctx, viewer_id, viewer_role, creator_id, strict, expected) -> None:
    viewer = json.dumps({"type": "user", "id": viewer_id, "display": "X", "role": viewer_role, "source": "local"})
    session = json.dumps({"id": "s1", "initiated_by": {"type": "user", "id": creator_id, "display": "C", "role": "user", "source": "local"}})
    settings = json.dumps({"strict_mode": strict})
    got = js(ctx, "NV_canvasRunHtml(" + viewer + ", " + session + ", " + settings + ")")
    assert got is expected


# --- ruling 2: window messages --------------------------------------------------


def test_the_window_listener_accepts_only_the_readiness(ctx) -> None:
    # `FW` is a stand-in for the renderer frame's own window (event.source).
    # before the handshake: only the renderer's own `ready` reply, from the renderer frame's window
    ready = js(ctx, "NV_canvasWindowMessage({source: 'FW', data: {type: 'ready', protocol: 1}}, 'FW', false)")
    assert ready == "ready"
    # a forged state from an Html sub-frame (a different source) is dropped
    forged_state = js(ctx, "NV_canvasWindowMessage({source: 'HTML_SUBFRAME', data: {type: 'state', rev: 9, elements: {}}}, 'FW', false)")
    assert forged_state is None
    # a forged value/event from the renderer frame itself is still dropped (not a ready)
    forged_value = js(ctx, "NV_canvasWindowMessage({source: 'FW', data: {type: 'event', element: 'a', kind: 'click'}}, 'FW', false)")
    assert forged_value is None
    # after the handshake: the port owns everything; even a ready from the frame is dropped on window
    after = js(ctx, "NV_canvasWindowMessage({source: 'FW', data: {type: 'ready', protocol: 1}}, 'FW', true)")
    assert after is None


# --- the phone: a chip in the header, a top sheet instead of a split -----------


def test_a_phone_shows_a_chip_not_a_split(ctx) -> None:
    """Mobile, count 3, state null: the header's chip reads 'Canvas · 3' and the
    panel draws neither the split (nv-canvas) nor the sheet (nv-canvas-sheet) - a
    phone shows the chip only until the person opens it."""
    ctx.eval("window.__mobile = true;")
    ctx.eval("window.__con.canvas = null;")
    ctx.eval("window.__resources['nv-canvas:s1'] = " + PLAYLIST_VIEW + ";")
    ctx.eval("MR.mount(window.NV_CanvasChip, {count: 3, onOpen: function () { window.__chipOpened = true; }});")
    chip = js(ctx, 'MR.find("nv-canvas-chip")')
    assert chip is not None
    assert js(ctx, 'MR.find("nv-canvas-chip").props["data-verb"]') == "canvas.show"
    assert any("Canvas · 3" in t for t in js(ctx, "MR.texts()"))
    # the panel on a phone, not opened: no split, no sheet
    ctx.eval("MR.mount(window.NV_CanvasPanel, {sid: 's1', wid: 'w1', session: " + SESSION + ", focused: true});")
    assert js(ctx, 'MR.find("nv-canvas")') is None
    assert js(ctx, 'MR.find("nv-canvas-sheet")') is None


def test_the_chip_opens_the_top_sheet(ctx) -> None:
    """Clicking the chip sets the canvas state to 'open'; on a phone with state
    'open' the sheet holds the frame and carries a close affordance."""
    ctx.eval("window.__mobile = true;")
    ctx.eval("window.__con.canvas = null;")
    ctx.eval("window.__resources['nv-canvas:s1'] = {data: " + PLAYLIST_VIEW + ", error: null, loading: false, degraded: false, refetch: function(){}};")
    ctx.eval("MR.mount(window.NV_CanvasChip, {count: 3, onOpen: function () { window.__con.setCanvas('open'); }});")
    ctx.eval('MR.click("nv-canvas-chip")')
    assert js(ctx, "window.__setCanvas") == "open"
    # with state 'open' the sheet (not the split) holds the frame
    ctx.eval("window.__con.canvas = 'open';")
    ctx.eval("MR.mount(window.NV_CanvasPanel, {sid: 's1', wid: 'w1', session: " + SESSION + ", focused: true});")
    assert js(ctx, '!!MR.find("nv-canvas-sheet")') is True
    assert js(ctx, 'MR.find("nv-canvas")') is None
    assert js(ctx, '!!MR.find("nv-canvas-frame")') is True
    close = js(ctx, 'MR.find("nv-canvas-sheet-close")')
    assert close is not None
    assert js(ctx, 'MR.find("nv-canvas-sheet-close").props["data-verb"]') == "canvas.hide"


def test_closing_the_sheet_returns_to_automatic(ctx) -> None:
    """The sheet's close button sets the canvas state back to null, so the phone
    falls back to automatic (hidden until the person opens it again)."""
    ctx.eval("window.__mobile = true;")
    ctx.eval("window.__con.canvas = 'open';")
    ctx.eval("window.__resources['nv-canvas:s1'] = {data: " + PLAYLIST_VIEW + ", error: null, loading: false, degraded: false, refetch: function(){}};")
    ctx.eval("MR.mount(window.NV_CanvasPanel, {sid: 's1', wid: 'w1', session: " + SESSION + ", focused: true});")
    assert js(ctx, '!!MR.find("nv-canvas-sheet")') is True
    ctx.eval('MR.click("nv-canvas-sheet-close")')
    assert js(ctx, "window.__setCanvas") is None
    # a not-opened phone again draws no sheet
    ctx.eval("window.__con.canvas = null;")
    ctx.eval("MR.rerender({sid: 's1', wid: 'w1', session: " + SESSION + ", focused: true});")
    assert js(ctx, 'MR.find("nv-canvas-sheet")') is None
