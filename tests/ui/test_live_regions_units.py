"""The live-region rules, run as code in V8 (review of PR 517: four behaviours were pinned only by the browser journey).

``tests/ui_e2e/test_live_regions_journey.py`` drives the real components in a browser, but a reviewer without Playwright cannot run it, and
a mutant that breaks one of these rules passed every unit test. These run the REAL source of each piece in the V8 stand-in for React
(``tests/ui/_mini_react.py``):

* ``NV_useFirstLoadKeys`` freezes the keys the first load brought (history) and never moves them, and ``NV_arrivedLive`` says a key is news only
  after that load and only when it was not in it;
* ``NV_StatusStrip`` is not itself a live region, its live region holds the WORDS only, and the clock that ticks every second sits outside it;
* ``NV_ToastHost`` draws two sibling regions inside a stack that is not one itself: polite for news, assertive for errors, and a toast has no
  role of its own (an alert nested in a status region is announced twice).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")
SHELL = (ROOT / "ui" / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")
STATUS_JS = (ROOT / "ui" / "foundation" / "shell-status.js").read_text(encoding="utf-8")


def _function_source(text: str, name: str) -> str:
    start = text.index("function " + name + "(")
    return text[start:text.index("\n}\n", start) + len("\n}\n")]


def _transpiled(source: str) -> str:
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    bundler = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    try:
        return bundler._transform(source, "snippet.jsx")
    finally:
        bundler._ctx.close()


_TEXT = """
function __textOf(n) {
  if (n == null || typeof n === "boolean") return "";
  if (typeof n === "string" || typeof n === "number") return String(n);
  if (Array.isArray(n)) return n.map(__textOf).join("");
  if (n.__el) return __textOf(typeof n.type === "function" ? n.out : n.children);
  return "";
}
"""


@pytest.fixture
def make():
    made = []

    def build(source: str, prelude: str = ""):
        ctx = mini_react_context(_transpiled(source), _TEXT + prelude)
        made.append(ctx)
        return ctx

    yield build
    for ctx in made:
        ctx.close()


# --- the first-load keys and the arrival check ---------------------------------------------------------------------------------------

_KEYS_SOURCE = _function_source(DOC, "NV_useFirstLoadKeys") + _function_source(DOC, "NV_arrivedLive")
_PROBE = """
function Probe(props) {
  var base = NV_useFirstLoadKeys(props.loaded, props.keys);
  var live = {};
  (props.check || []).forEach(function (k) { live[k] = NV_arrivedLive(base, k); });
  globalThis.__probe = { base: base, live: live };
  return null;
}
"""


def _probe(ctx, props: dict) -> dict:
    ctx.eval("MR.rerender(" + json.dumps(props) + ")")
    return json.loads(ctx.eval("JSON.stringify(globalThis.__probe)"))


def test_nothing_is_live_before_the_first_load_has_arrived(make) -> None:
    ctx = make(_KEYS_SOURCE + _PROBE)
    ctx.eval('MR.mount(Probe, { loaded: false, keys: [], check: [1, 2] })')
    got = json.loads(ctx.eval("JSON.stringify(globalThis.__probe)"))
    assert got["base"] is None and got["live"] == {"1": False, "2": False}, "a record that is simply not loaded yet is not news"


def test_the_first_load_is_history_and_what_comes_after_is_news(make) -> None:
    ctx = make(_KEYS_SOURCE + _PROBE)
    ctx.eval('MR.mount(Probe, { loaded: false, keys: [], check: [] })')
    got = _probe(ctx, {"loaded": True, "keys": [1, 2, 3], "check": [1, 2, 3, 4]})
    assert got["base"] == {"1": True, "2": True, "3": True}
    assert got["live"] == {"1": False, "2": False, "3": False, "4": True}


def test_the_first_load_keys_are_frozen_so_a_later_arrival_stays_news(make) -> None:
    """If the baseline followed the data, a failure would be news on the render it arrived and history on the next."""
    ctx = make(_KEYS_SOURCE + _PROBE)
    ctx.eval('MR.mount(Probe, { loaded: true, keys: [1, 2, 3], check: [] })')
    got = _probe(ctx, {"loaded": True, "keys": [1, 2, 3, 4], "check": [1, 4]})
    assert got["base"] == {"1": True, "2": True, "3": True}, "the baseline did not move when key 4 arrived"
    assert got["live"] == {"1": False, "4": True}
    again = _probe(ctx, {"loaded": True, "keys": [1, 2, 3, 4, 5], "check": [4, 5]})
    assert again["live"] == {"4": True, "5": True}


def test_a_session_opened_with_history_alerts_nothing_until_something_arrives(make) -> None:
    ctx = make(_KEYS_SOURCE + _PROBE)
    ctx.eval('MR.mount(Probe, { loaded: true, keys: [1, 2, 3], check: [1, 2, 3] })')
    got = json.loads(ctx.eval("JSON.stringify(globalThis.__probe)"))
    assert not any(got["live"].values()), "three past failures must not be three announcements"


def test_a_key_that_is_not_in_the_baseline_is_live_and_one_that_is_is_not(make) -> None:
    ctx = make(_KEYS_SOURCE)
    assert ctx.eval("NV_arrivedLive({ 7: true }, 7)") is False
    assert ctx.eval("NV_arrivedLive({ 7: true }, 8)") is True
    assert ctx.eval("NV_arrivedLive(null, 8)") is False


# --- the status strip: the clock is not in the live region ---------------------------------------------------------------------------

_STRIP_PRELUDE = """
var __intervals = [];
globalThis.setInterval = function (fn, ms) { __intervals.push({ fn: fn, ms: ms, cleared: false }); return __intervals.length; };
globalThis.clearInterval = function (id) { if (__intervals[id - 1]) __intervals[id - 1].cleared = true; };
var __now = 1000000;
Date.now = function () { return __now; };
"""


@pytest.fixture
def strip(make):
    ctx = make(_function_source(DOC, "NV_StatusStrip"), _STRIP_PRELUDE)
    ctx.eval(STATUS_JS)
    return ctx


def _live_and_clock(ctx) -> dict:
    return json.loads(ctx.eval("""JSON.stringify({
      strip: MR.find("nv-status-strip") ? { role: MR.find("nv-status-strip").props.role === undefined ? null : MR.find("nv-status-strip").props.role } : null,
      live: MR.find("nv-status-live") ? { role: MR.find("nv-status-live").props.role, text: __textOf(MR.find("nv-status-live")) } : null,
      clock: MR.find("nv-status-clock") ? __textOf(MR.find("nv-status-clock")) : null,
    })"""))


def test_the_strip_is_not_a_live_region_and_its_words_are(strip) -> None:
    strip.eval('MR.mount(NV_StatusStrip, { shown: { verb: "thinking", object: "", startedMs: __now - 3000 }, canStop: true, stopping: false })')
    got = _live_and_clock(strip)
    assert got["strip"] == {"role": None}, "a status role on the whole strip re-reads the ticking clock every second"
    assert got["live"]["role"] == "status" and got["live"]["text"] == "running: thinking"
    assert got["clock"] is not None and "3s" in got["clock"], got


def test_the_words_never_contain_the_clock_and_do_not_change_while_it_ticks(strip) -> None:
    strip.eval('MR.mount(NV_StatusStrip, { shown: { verb: "thinking", object: "", startedMs: __now }, canStop: false, stopping: false })')
    first = _live_and_clock(strip)
    strip.eval("__now += 4000; __intervals[0].fn(); MR.rerender();")
    later = _live_and_clock(strip)
    assert first["live"]["text"] == later["live"]["text"] == "running: thinking", "the live region's text is the same after four ticks"
    assert first["clock"] != later["clock"], "while the clock beside it moved on"
    assert not any(ch.isdigit() for ch in later["live"]["text"])


def test_a_change_of_state_changes_the_live_region_and_drops_the_clock(strip) -> None:
    strip.eval('MR.mount(NV_StatusStrip, { shown: { verb: "thinking", object: "", startedMs: __now }, canStop: true, stopping: false })')
    strip.eval('MR.rerender({ shown: { verb: "thinking", object: "", startedMs: __now }, canStop: true, stopping: true })')
    got = _live_and_clock(strip)
    assert got["live"]["text"] == "stopping" and got["clock"] is None


def test_the_strips_ticker_is_cleared_when_it_goes(strip) -> None:
    strip.eval('MR.mount(NV_StatusStrip, { shown: { verb: "thinking", object: "", startedMs: __now }, canStop: false, stopping: false })')
    assert strip.eval("__intervals.length") == 1 and strip.eval("__intervals[0].ms") == 1000


# --- the toast host: two sibling regions, no role of its own --------------------------------------------------------------------------

_TOAST_PRELUDE = """
globalThis.setTimeout = function () { return 1; };
window.primerApi = {};
"""

_TOAST_SOURCE = _function_source(SHELL, "NV_ToastHost")


@pytest.fixture
def toasts(make):
    ctx = make(_TOAST_SOURCE, _TOAST_PRELUDE)
    ctx.eval("MR.mount(NV_ToastHost, {})")
    return ctx


def _regions(ctx) -> dict:
    return json.loads(ctx.eval("""JSON.stringify({
      stack: { role: MR.find("nv-toasts").props.role === undefined ? null : MR.find("nv-toasts").props.role,
               live: MR.find("nv-toasts").props["aria-live"] === undefined ? null : MR.find("nv-toasts").props["aria-live"] },
      status: { role: MR.find("nv-toasts-status").props.role, live: MR.find("nv-toasts-status").props["aria-live"], text: __textOf(MR.find("nv-toasts-status")) },
      alert: { role: MR.find("nv-toasts-alert").props.role, text: __textOf(MR.find("nv-toasts-alert")) },
    })"""))


def test_the_stack_is_not_a_live_region_and_the_two_regions_exist_before_any_toast(toasts) -> None:
    got = _regions(toasts)
    assert got["stack"] == {"role": None, "live": None}
    assert got["status"]["role"] == "status" and got["status"]["live"] == "polite" and got["status"]["text"] == ""
    assert got["alert"]["role"] == "alert" and got["alert"]["text"] == "", "both regions are in the page first, so a toast inserted into either is announced"


def test_news_goes_to_the_polite_region_and_an_error_to_the_assertive_one_and_never_both(toasts) -> None:
    toasts.eval('window.primerApi.toastPush({ kind: "success", text: "Saved the thing" }); MR.rerender();')
    toasts.eval('window.primerApi.toastPush({ kind: "error", text: "Could not save" }); MR.rerender();')
    toasts.eval('window.primerApi.toastPush({ text: "No kind given" }); MR.rerender();')
    got = _regions(toasts)
    assert "Saved the thing" in got["status"]["text"] and "No kind given" in got["status"]["text"]
    assert "Could not save" in got["alert"]["text"]
    assert "Could not save" not in got["status"]["text"] and "Saved the thing" not in got["alert"]["text"]


def test_a_toast_has_no_role_of_its_own(toasts) -> None:
    toasts.eval('window.primerApi.toastPush({ kind: "error", text: "Could not save" }); MR.rerender();')
    roles = json.loads(toasts.eval("""(function () {
      var out = [];
      (function walk(n) {
        if (n == null || typeof n !== "object") return;
        if (Array.isArray(n)) { n.forEach(walk); return; }
        if (!n.__el) return;
        if (n.props && typeof n.props.className === "string" && n.props.className.indexOf("toast ") === 0) out.push(n.props.role === undefined ? null : n.props.role);
        walk(typeof n.type === "function" ? n.out : n.children);
      })(MR.find("nv-toasts"));
      return JSON.stringify(out);
    })()"""))
    assert roles == [None], f"a toast with its own role nested in a live region is announced twice: {roles}"
