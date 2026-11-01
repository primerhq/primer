"""A workspace that does not exist is asked about once and then left alone, its tap included (C-019 after UI-L1).

UI-L1 made the tap hub (``ui/foundation/use-workspace-tap.js``) reopen an EventSource the browser closed for good while any consumer
still holds it, backing off 1 s to 60 s. The tap of a missing workspace answers 404, which closes it for good too, and the Studio's rail
(always mounted, beside the not-found card) kept holding that workspace's hub through its attention listener: the console asked for
``/v1/workspaces/<missing>/tap`` again after 1, 3, 7 s ... (``tests/ui_e2e/test_unknown_workspace_journey.py``). Once the workspace row
says 404, nothing holds the missing workspace's tap.

The real Studio, rail, hub, ``api.js``, ``use-resource.js`` and ``sh-api.jsx`` on the hook runtime (``tests/ui/_mini_react.py``), against a
fake ``apiFetch`` and a fake ``EventSource``; timers are recorded, not run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
FOUNDATION = [(UI / "foundation" / f).read_text(encoding="utf-8") for f in ("api.js", "use-resource.js", "use-workspace-tap.js")]


def _sources(*components: str) -> str:
    return "\n".join([*FOUNDATION, *(transpile(UI / "components" / c) for c in components)])

_PRELUDE = r"""
var window = globalThis;
window.innerWidth = 1280; window.innerHeight = 800;
var TIMERS = []; var TID = 0;
function setTimeout(fn, ms) { TID += 1; TIMERS.push({ id: TID, fn: fn, ms: ms }); return TID; }
function clearTimeout(id) { TIMERS = TIMERS.filter(function (t) { return t.id !== id; }); }
function AbortController() { var s = { aborted: false }; this.signal = s; this.abort = function () { s.aborted = true; }; }
var document = { hidden: false, title: "", addEventListener: function () {}, removeEventListener: function () {}, body: { style: {} } };
window.addEventListener = function () {}; window.removeEventListener = function () {};
var OPENED = [];
function EventSource(url) { this.url = url; this.readyState = 0; OPENED.push(this); }
EventSource.prototype.close = function () { this.readyState = 2; };
window.NV_TabGroups = function () { return null; };
var CON = { wid: "nope", registry: { get: function () { return null; } }, panels: {}, workspaces: [{ id: "w-0", name: "Alpha" }] };
function NV_useConsole() { return CON; }
"""

_SERVER = r"""
window.primerApi.apiFetch = function (method, path) {
  if (path === "/workspaces/nope") return Promise.reject(new window.primerApi.ApiError({ status: 404, title: "Not Found" }));
  if (/^\/workspaces\?/.test(path)) return Promise.resolve({ kind: "offset", offset: 0, length: 1, total: 1, items: [{ id: "w-0", name: "Alpha" }] });
  return Promise.resolve({ items: [] });
};
window.primerApi.useEscape = function () {};
window.primerApi.useFocusTrap = function () {};
function tapsOf(wid) { return OPENED.filter(function (es) { return es.url.indexOf("/v1/workspaces/" + wid + "/tap") === 0; }); }
function refuse(es) { es.readyState = 2; es.onerror(); }
"""


@pytest.fixture
def studio():
    ctx = mini_react_context(_sources("shell/sh-api.jsx", "console/nv-rail.jsx", "console/nv-studio.jsx"), _PRELUDE)
    ctx.eval(_SERVER)
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def phone():
    """The phone shell, its tabs not drawn (the shell-level tap listener is the subject)."""
    stubs = "window.MobileTabs = function () { return null; }; CON.closeOverlay = function () {}; function NV_useThemeRestore() {}"
    ctx = mini_react_context(_sources("shell/sh-api.jsx", "console/nv-studio.jsx", "console/nv-mobile-shell.jsx"), _PRELUDE + stubs)
    ctx.eval(_SERVER)
    try:
        yield ctx
    finally:
        ctx.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def show_the_card(ctx) -> None:
    """Mount, let the workspace row's 404 land (microtasks drain between evals), and draw the card."""
    ctx.eval("MR.mount(NV_Studio, {});")
    for _ in range(6):
        ctx.eval("MR.rerender();")
    assert js(ctx, "MR.find('nv-ws-gone') !== null"), "the not-found card is up"


def test_nothing_holds_the_missing_workspaces_tap_once_the_card_is_up(studio) -> None:
    show_the_card(studio)
    assert js(studio, "WTAP_hubConnState('nope')") is None, "a mounted component still holds the missing workspace's tap"


def test_a_refused_tap_of_the_missing_workspace_is_not_asked_for_again(studio) -> None:
    """The first render asks optimistically (nothing waits on the row); its 404 closes the EventSource, and no backoff reopens it."""
    show_the_card(studio)
    first = js(studio, "tapsOf('nope').length")
    assert first <= 1
    studio.eval("tapsOf('nope').forEach(refuse); TIMERS.slice().forEach(function (t) { t.fn(); });")
    assert js(studio, "tapsOf('nope').length") == first, "the missing workspace's tap was asked for again after the card"


def test_a_real_workspace_keeps_its_tap(studio) -> None:
    """Characterization: a workspace whose row answers keeps the rail's tap (the fix only lets go of a 404)."""
    studio.eval("CON.wid = 'w-0'; window.primerApi.apiFetch = (function (prev) { return function (m, p) {"
                " return p === '/workspaces/w-0' ? Promise.resolve({ id: 'w-0', name: 'Alpha' }) : prev(m, p); }; })(window.primerApi.apiFetch);")
    studio.eval("MR.mount(NV_Studio, {});")
    for _ in range(6):
        studio.eval("MR.rerender();")
    assert js(studio, "MR.find('nv-ws-gone')") is None
    assert js(studio, "WTAP_hubConnState('w-0')") is not None


def test_the_phone_shell_lets_go_of_the_missing_workspaces_tap(phone) -> None:
    """The phone shell's attention nudge listens on the workspace's tap at shell level, beside every tab: the same 404 lets go of it."""
    phone.eval("MR.mount(NV_MobileShell, {});")
    for _ in range(6):
        phone.eval("MR.rerender();")
    assert js(phone, "WTAP_hubConnState('nope')") is None, "the phone shell still holds the missing workspace's tap"
