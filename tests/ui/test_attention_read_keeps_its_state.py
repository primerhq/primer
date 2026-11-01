"""The one attention read keeps what it shows when the workspace list arrives (the phone Inbox's Try again flake after T87).

T87 (UI-L11) moved every reader of ``/yields/pending`` onto ``SH_useAttention`` (``ui/components/shell/sh-api.jsx``), and put the workspace
ids into the cache key (``deps: [ids.join(",")]``) for the per-workspace fallback's sake. The aggregate does not depend on them, but a new key is
a new cache entry: when the workspace list landed (or changed) after the attention read had failed, the phone Inbox dropped its error, showed
"Loading" and read the aggregate again. In ``tests/ui_e2e/test_mobile_inbox_empty_journey.py`` the list landed after the 503, Try again was
detached from the DOM, the second read answered 200 and the button never came back (CI on 3676da093).

The key is the aggregate's own now; the fallback (a server that 404s the aggregate) still reads the workspaces again when their list changes.
The real phone shell, ``use-resource.js`` and ``sh-api.jsx`` run on the hook runtime (``tests/ui/_mini_react.py``) against a fake
``apiFetch``; timers are recorded and run by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
FOUNDATION = [(UI / "foundation" / f).read_text(encoding="utf-8") for f in ("api.js", "use-resource.js", "use-workspace-tap.js", "shell-attention.js")]
COMPONENTS = ("shell/sh-api.jsx", "console/nv-studio.jsx", "shared/mobile-tabs.jsx", "console/nv-mobile-shell.jsx")

_PRELUDE = r"""
var window = globalThis;
window.innerWidth = 390; window.innerHeight = 844;
var TIMERS = []; var TID = 0;
function setTimeout(fn, ms) { TID += 1; TIMERS.push({ id: TID, fn: fn, ms: ms }); return TID; }
function clearTimeout(id) { TIMERS = TIMERS.filter(function (t) { return t.id !== id; }); }
function runTimers() { var due = TIMERS; TIMERS = []; due.forEach(function (t) { t.fn(); }); }
function AbortController() { var s = { aborted: false }; this.signal = s; this.abort = function () { s.aborted = true; }; }
var document = { hidden: false, title: "", addEventListener: function () {}, removeEventListener: function () {}, body: { style: {} } };
window.addEventListener = function () {}; window.removeEventListener = function () {};
function EventSource(url) { this.url = url; this.readyState = 0; }
EventSource.prototype.close = function () { this.readyState = 2; };
window.NV_TabGroups = function () { return null; };
// The workspace list has not landed yet: the shell hands the phone an empty list until it does.
var CON = { wid: null, workspaces: [], view: { name: "studio" }, doc: null, overlay: null, closeOverlay: function () {}, registry: { get: function () { return null; } } };
function NV_useConsole() { return CON; }
function NV_useThemeRestore() {}
function NV_errText(e) { return String((e && (e.detail || e.message)) || e); }
"""

_SERVER = r"""
var READS = [];
var AGGREGATE = 503;
window.primerApi.apiFetch = function (method, path) {
  READS.push(path);
  if (path === "/yields/pending") {
    if (AGGREGATE === 200) return Promise.resolve({ items: [], total: 0 });
    return Promise.reject(new window.primerApi.ApiError({ status: AGGREGATE, title: "Unavailable", detail: "The server is starting up." }));
  }
  var ws = /^\/workspaces\/([^/]+)\/yields\/pending$/.exec(path);
  if (ws) return Promise.resolve({ items: [{ session_id: "s-" + ws[1], kind: "approval" }] });
  return Promise.resolve({ items: [] });
};
window.primerApi.useEscape = function () {};
window.primerApi.useFocusTrap = function () {};
function reads(path) { return READS.filter(function (p) { return p === path; }).length; }
// Every state the Inbox panel was drawn in, in order (the panel is a global the shell reads when it renders).
var DRAWN = [];
var realPanel = NV_MobileInboxPanel;
NV_MobileInboxPanel = function (p) {
  DRAWN.push(p.items.length ? "items" : !p.loaded && p.error ? "error" : !p.loaded ? "loading" : "empty");
  return realPanel(p);
};
function landWorkspaces(list) { CON = Object.assign({}, CON, { wid: list[0].id, workspaces: list }); MR.rerender(); }
"""


@pytest.fixture
def phone():
    code = "\n".join([*FOUNDATION, *(transpile(UI / "components" / c) for c in COMPONENTS)])
    ctx = mini_react_context(code, _PRELUDE)
    ctx.eval(_SERVER)
    try:
        yield ctx
    finally:
        ctx.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def settle(ctx) -> None:
    for _ in range(6):  # microtasks drain between evals
        ctx.eval("MR.settle();")


def drawn_since(ctx, mark: int) -> list[str]:
    return js(ctx, "DRAWN.slice(" + str(mark) + ")")


def test_a_failed_read_keeps_its_error_when_the_workspace_list_lands(phone) -> None:
    phone.eval("MR.mount(NV_MobileShell, {});")
    settle(phone)
    assert js(phone, "!!MR.find('nv-mob-ib-retry')"), "the 503 is shown with Try again"
    assert js(phone, "reads('/yields/pending')") == 1
    mark = js(phone, "DRAWN.length")

    phone.eval("landWorkspaces([{ id: 'w-0', name: 'Alpha' }, { id: 'w-1', name: 'Beta' }]);")
    settle(phone)

    assert set(drawn_since(phone, mark)) == {"error"}, "the error block was replaced while nobody asked for a new read"
    assert js(phone, "reads('/yields/pending')") == 1, "the workspace list is not a reason to read the aggregate again"

    # Time passes: the backed-off poll reads once more, and the error stays put; nothing else reads it.
    phone.eval("runTimers();")
    settle(phone)
    assert js(phone, "reads('/yields/pending')") == 2
    assert set(drawn_since(phone, mark)) == {"error"}

    # Try again is the read that recovers.
    phone.eval("AGGREGATE = 200; MR.click('nv-mob-ib-retry');")
    settle(phone)
    assert js(phone, "!!MR.find('nv-mob-ib-empty')")


def test_the_per_workspace_fallback_reads_again_when_the_workspace_list_changes(phone) -> None:
    """A server without the aggregate (404) is read per workspace: a list that lands or changes is read again, keeping what is shown meanwhile."""
    phone.eval("AGGREGATE = 404; MR.mount(NV_MobileShell, {});")
    settle(phone)
    assert js(phone, "!!MR.find('nv-mob-ib-empty')"), "no workspace known yet: nothing to read per workspace"

    phone.eval("landWorkspaces([{ id: 'w-0', name: 'Alpha' }]);")
    settle(phone)
    assert js(phone, "reads('/workspaces/w-0/yields/pending')") == 1
    assert js(phone, "DRAWN[DRAWN.length - 1]") == "items", "w-0's parked session is shown"
    mark = js(phone, "DRAWN.length")

    phone.eval("landWorkspaces([{ id: 'w-0', name: 'Alpha' }, { id: 'w-1', name: 'Beta' }]);")
    settle(phone)
    assert js(phone, "reads('/workspaces/w-1/yields/pending')") == 1
    assert "loading" not in drawn_since(phone, mark), "the rows already shown stay while the new list is read"


def test_a_shown_queue_is_read_again_when_a_workspace_appears(phone) -> None:
    """With an answer on screen, a workspace that appears is read again in place: its sessions may be waiting (the desktop rail re-read on a
    changed workspace list before T87; tests/ui_e2e/test_ask_user_panel.py u0048 relies on it after a hash-only navigation)."""
    phone.eval("AGGREGATE = 200; MR.mount(NV_MobileShell, {});")
    settle(phone)
    assert js(phone, "reads('/yields/pending')") == 1
    mark = js(phone, "DRAWN.length")

    phone.eval("landWorkspaces([{ id: 'w-0', name: 'Alpha' }]);")
    settle(phone)

    assert js(phone, "reads('/yields/pending')") == 2, "a new workspace is a reason to read the queue again"
    assert "loading" not in drawn_since(phone, mark), "the answer already shown stays while it is read again"
