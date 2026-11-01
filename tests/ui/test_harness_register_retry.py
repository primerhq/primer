"""The Register-harness dialog and the harness pages after a failure (PERF/UI task 86: UI-M8, UI-L7, UI-L8, UI-L9b, UI-L9c).

The REAL ``HarnessRegisterDialog`` runs in V8 on the mini React with the real ``ui/foundation/api.js``; only the network (a small stateful server that
refuses a second harness with a slug already taken, as ``primer/api/routers/harness.py`` does), the shared widgets and the timers are stubbed. The pages
that need the router, the paged list and the console are checked in the source, as the rest of this suite does for ``harnesses.jsx``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
HARNESSES = UI / "components" / "harnesses.jsx"
BUILDER = UI / "components" / "harness_outbound_builder.jsx"
COLLECTIONS = UI / "components" / "internal-collections.jsx"
API = (UI / "foundation" / "api.js").read_text(encoding="utf-8")

PRELUDE = r"""
var TIMERS = [];
window.setTimeout = function (fn, ms) { TIMERS.push(fn); return TIMERS.length; };
window.clearTimeout = function () {};
function textOf(n) {
  if (n == null || typeof n === "boolean") return "";
  if (typeof n === "string" || typeof n === "number") return String(n);
  if (Array.isArray(n)) return n.map(textOf).join("");
  return n.__el ? textOf(n.children) : "";
}
window.Btn = function (p) { return React.createElement("button", { "data-testid": "btn:" + textOf(p.children), onClick: p.onClick, disabled: p.disabled }, p.children); };
window.Modal = function (p) { return React.createElement("div", { "data-testid": "modal" }, p.children, p.footer); };
window.Banner = function (p) { return React.createElement("div", { "data-testid": "banner", "data-kind": p.kind }, p.title, p.detail); };
window.Icon = function () { return null; };
window.JsonSchemaForm = function () { return null; };
window.validateSchema = function () { return []; };
"""

DRIVER = r"""
var CALLS = [], ROWS = {}, NEXT = 1, GET_ANSWER = null, ERROR_ON_FETCH = true;
function refuse(status, title) { var e = new window.primerApi.ApiError({ status: status, title: title, detail: title, extensions: {} }); return Promise.reject(e); }
window.primerApi.apiFetch = function (method, path, body) {
  CALLS.push(method + " " + path);
  if (method === "POST" && path === "/harnesses") {
    for (var k in ROWS) if (ROWS[k].slug === body.slug) return refuse(409, "A harness with slug '" + body.slug + "' already exists");
    var id = "h" + (NEXT++);
    ROWS[id] = { id: id, slug: body.slug, git_url: body.git_url, status: "DRAFT", pending_operation: null };
    return Promise.resolve(JSON.parse(JSON.stringify(ROWS[id])));
  }
  var m = path.match(/^\/harnesses\/([^\/?]+)(\/fetch)?(\?.*)?$/);
  if (method === "POST" && m && m[2]) { ROWS[m[1]].pending_operation = "FETCH"; return Promise.resolve({}); }
  // PUT /harnesses/{id}: a partial update of the row (the real route, update_harness in primer/api/routers/harness.py)
  if (method === "PUT" && m && !m[2]) {
    for (var f in body) if (body[f] != null) ROWS[m[1]][f] = body[f];
    return Promise.resolve(JSON.parse(JSON.stringify(ROWS[m[1]])));
  }
  if (method === "GET" && m) {
    if (GET_ANSWER) return Promise.resolve(GET_ANSWER(ROWS[m[1]]));
    var r = ROWS[m[1]]; r.pending_operation = null;
    r.status = ERROR_ON_FETCH ? "ERROR" : "READY"; r.last_operation_error = ERROR_ON_FETCH ? "could not clone" : null;
    return Promise.resolve(JSON.parse(JSON.stringify(r)));
  }
  // DELETE /harnesses/{id} is NOT a delete: it enqueues an UNINSTALL and answers 202; the row and its slug stay until a worker runs it.
  if (method === "DELETE" && m) { ROWS[m[1]].pending_operation = "UNINSTALL"; return Promise.resolve({}); }
  return Promise.resolve({});
};
var CLOSED = 0, CREATED = [];
function mountDialog() { MR.mount(window.HarnessRegisterDialog, { onClose: function () { CLOSED += 1; }, onCreated: function (h) { CREATED.push(h); } }); }
function typeInto(id, value) { MR.findProp("id", id).props.onChange({ target: { value: value } }); MR.rerender(); }
function btn(label) { var n = MR.find("btn:" + label); if (!n) throw new Error("no button " + label); return n; }
function click(label) { var out = btn(label).props.onClick({}); MR.rerender(); return out; }
function banners() { return MR.findAll("banner").map(function (b) { return textOf(b); }); }
function runTimers() { var t = TIMERS; TIMERS = []; t.forEach(function (f) { f(); }); }
"""


@pytest.fixture
def ctx():
    c = mini_react_context(API + "\n" + transpile(HARNESSES), PRELUDE)
    c.eval(DRIVER)
    c.eval('mountDialog(); typeInto("hr-name", "My harness"); typeInto("hr-git-url", "https://example.com/x.git");')
    yield c
    c.close()


def _settle(ctx) -> None:
    for _ in range(6):
        ctx.eval("void 0;")
        ctx.eval("MR.rerender();")


def _calls(ctx) -> list[str]:
    import json

    return json.loads(ctx.eval("JSON.stringify(CALLS)"))


def test_a_failed_fetch_then_a_second_press_with_the_same_slug_updates_and_fetches_the_same_row(ctx) -> None:
    ctx.eval('click("Fetch");')
    _settle(ctx)
    assert "Fetch failed" in ctx.eval("banners().join('|')")
    ctx.eval("typeInto('hr-git-url', 'https://example.com/fixed.git'); click('Fetch');")
    _settle(ctx)
    calls = _calls(ctx)
    assert calls.count("POST /harnesses") == 1 and not any(c.startswith("DELETE") for c in calls), calls
    assert calls.count("PUT /harnesses/h1") == 1 and calls.count("POST /harnesses/h1/fetch") == 2, calls
    assert "already exists" not in ctx.eval("banners().join('|')")
    assert ctx.eval("ROWS.h1.git_url") == "https://example.com/fixed.git"


def test_a_changed_slug_enqueues_the_old_rows_uninstall_and_creates_the_new_one(ctx) -> None:
    ctx.eval('click("Fetch");')
    _settle(ctx)
    ctx.eval("typeInto('hr-slug', 'other-slug'); click('Fetch');")
    _settle(ctx)
    calls = _calls(ctx)
    assert calls.count("POST /harnesses") == 2, calls
    assert [c for c in calls if c.startswith("DELETE")] == ["DELETE /harnesses/h1?cascade=false"], calls
    assert "already exists" not in ctx.eval("banners().join('|')")


def test_a_fixed_url_after_an_error_reaches_step_two(ctx) -> None:
    ctx.eval('click("Fetch");')
    _settle(ctx)
    ctx.eval("ERROR_ON_FETCH = false; typeInto('hr-git-url', 'https://example.com/fixed.git'); click('Fetch');")
    _settle(ctx)
    assert "fetched successfully" in ctx.eval("textOf(MR.find('modal'))")


def test_the_happy_register_flow_makes_the_same_requests_and_deletes_nothing(ctx) -> None:
    ctx.eval("ERROR_ON_FETCH = false; click('Fetch');")
    _settle(ctx)
    ctx.eval("GET_ANSWER = function (row) { return { id: row.id, status: 'INSTALLED', pending_operation: null }; }; click('Create');")
    _settle(ctx)
    assert _calls(ctx) == [
        "POST /harnesses", "POST /harnesses/h1/fetch", "GET /harnesses/h1",
        "PUT /harnesses/h1/overrides", "POST /harnesses/h1/install", "GET /harnesses/h1",
    ]
    assert ctx.eval("CREATED.length") == 1 and ctx.eval("CLOSED") == 0


def test_cancel_after_a_failed_fetch_enqueues_the_uninstall_of_the_row_that_never_installed(ctx) -> None:
    ctx.eval('click("Fetch");')
    _settle(ctx)
    ctx.eval('click("Cancel");')
    _settle(ctx)
    assert "DELETE /harnesses/h1?cascade=false" in _calls(ctx) and ctx.eval("CLOSED") == 1


def test_cancel_after_a_failed_install_keeps_the_row_and_its_error(ctx) -> None:
    ctx.eval("ERROR_ON_FETCH = false; click('Fetch');")
    _settle(ctx)
    ctx.eval("GET_ANSWER = function (row) { return { id: row.id, status: 'ERROR', pending_operation: null, last_operation_error: 'install broke' }; }; click('Create');")
    _settle(ctx)
    assert "install broke" in ctx.eval("banners().join('|')")
    ctx.eval('click("Cancel");')
    _settle(ctx)
    assert not any(c.startswith("DELETE") for c in _calls(ctx)) and ctx.eval("CLOSED") == 1


def test_closing_the_dialog_stops_the_poll_loop(ctx) -> None:
    ctx.eval("GET_ANSWER = function (row) { return { id: row.id, status: 'DRAFT', pending_operation: 'FETCH' }; };")
    ctx.eval('click("Fetch");')
    _settle(ctx)
    gets = [c for c in _calls(ctx) if c.startswith("GET ")]
    assert len(gets) == 1                                                  # polling, waiting on its timer
    ctx.eval('MR.mount(function () { return null; }, {});')                # the dialog leaves the tree
    for _ in range(3):
        ctx.eval("runTimers();")
        _settle(ctx)
    assert len([c for c in _calls(ctx) if c.startswith("GET ")]) == 1      # the loop did not ask again


# ---- the pages that need the router, the paged list and the console: source checks ----------------------------------------------------------------

def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function(src: str, name: str) -> str:
    start = src.index("function " + name + "(")
    return src[start:src.index("\n}\n", start) + 3]


def test_the_harness_detail_does_not_poll_with_set_interval() -> None:
    detail = _function(_src(HARNESSES), "HarnessDetail")
    assert "setInterval" not in detail
    assert "pollMs: pendingPoll ? 1000 : 0" in detail


def test_only_the_last_pressed_actions_error_is_shown() -> None:
    detail = _function(_src(HARNESSES), "HarnessDetail")
    assert "lastAction" in detail
    assert "fetchMut.error || syncMut.error" not in detail
    for action, mut in (("fetch", "fetchMut"), ("sync", "syncMut"), ("build", "buildMut"), ("push", "pushMut")):
        assert f'setLastAction("{action}"); {mut}.mutate()' in detail, action


def test_push_outbound_has_a_busy_guard_and_shows_its_error() -> None:
    lst = _function(_src(HARNESSES), "HarnessList")
    body = lst[lst.index("const onPushOutbound"):lst.index("return (")]
    assert "if (pushingId) return" in body and "setPushError(" in body
    assert re.search(r"catch \(_err\) \{\s*//", body) is None           # no empty swallow
    assert "harness-push-error" in lst and "disabled={pushingId != null}" in lst


def test_the_outbound_builder_reuses_the_harness_it_created_and_its_poll_stops_on_unmount() -> None:
    src = _src(BUILDER)
    assert "const existing = initialHarness || createdHarness;" in src
    assert src.count("isActive: () => mountedRef.current") == 2


def test_a_bootstrap_this_client_started_is_toasted_once() -> None:
    src = _src(COLLECTIONS)
    for card in ("ConfiguredCard", "ActiveCard"):
        body = src[src.index("function " + card + "("):]
        body = body[:body.index("\nfunction ", 10)] if "\nfunction " in body[10:] else body
        assert "ownRunUntil.current = Infinity" in body and "const own = ownRunUntil.current > Date.now();" in body, card
        effect = body[body.index("const own = "):body.index("prevStatusRef.current = curr;")]
        assert effect.index("if (own)") < effect.index('prev === "running" && curr === "succeeded"'), card


def test_a_failed_run_of_this_client_keeps_the_settle_window_and_only_a_409_clears_it() -> None:
    """A non-409 failure is toasted by onError; the poll then sees running to failed and must not toast it again. A 409 is a run started elsewhere."""
    src = _src(COLLECTIONS)
    for card in ("ConfiguredCard", "ActiveCard"):
        body = src[src.index("function " + card + "("):]
        on_error = body[body.index("onError: (err) => {"):]
        on_error = on_error[:on_error.index("\n      },\n")]
        before_409, after_409 = on_error.split("if (err?.status === 409) {")
        assert "ownRunUntil.current = Date.now() + IC_OWN_RUN_SETTLE_MS;" in before_409, card
        assert "ownRunUntil.current = 0;" in after_409.split("return;")[0], card
        assert "ownRunUntil.current = 0;" not in before_409, card
