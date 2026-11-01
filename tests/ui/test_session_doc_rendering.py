"""Session document rendering (Task 81): what a tap frame costs the open document.

UI-M1: the document read the tap through ``useWorkspaceTap``, the render hook, which re-renders its caller on every frame
of the workspace, any session's, to recompute one value (``live = SH_statusFromTap(...)``); each render re-parsed the
markdown of every assistant row. It now listens (``useWorkspaceTapListener``), reads the hub's buffer with
``WTAP_events``, and re-renders only when this session's live status changed; an assistant row's markdown is parsed
again only when its text changes (``NV_Markdown``, a ``React.memo``).

The real ``NV_SessionDoc`` runs on the ``tests/ui/_mini_react.py`` runtime with the real session store, tap hub and
``useSessionStore``; only the console handle, the resources (the session row and its history page are served) and the
``EventSource`` are stand-ins, and ``window.renderMarkdown`` counts its calls. ``MR.settle()`` renders only when a state
change is pending, as React does.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from tests.ui._mini_react import ROOT, mini_react_context, transpile

UI = ROOT / "ui"
DOC_PATH = UI / "components" / "console" / "nv-session-doc.jsx"
DOC = DOC_PATH.read_text(encoding="utf-8")


def _src(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


STUBS = r"""
var CON = { wid: "w1", toast: function () {}, setDoc: function () {}, voiceRef: { current: null }, registry: SH_createVerbRegistry() };
function NV_useConsole() { return CON; }
function NV_identity() { return { color: "currentColor", d: "" }; }
function setInterval() { return 0; } function clearInterval() {}
function setTimeout() { return 0; } function clearTimeout() {}
var document = { body: { style: {} } };
function Icon() { return null; }
var OPENED = [];
function EventSource(url) { this.url = url; this.readyState = 0; OPENED.push(this); }
EventSource.prototype.close = function () { this.readyState = 2; };
var MD = 0;
window.renderMarkdown = function (t) { MD += 1; return React.createElement("p", { className: "md" }, t); };
var SESSION_ROW = { session_id: "s1", status: "active", turn_status: "idle", binding: { kind: "agent", agent_id: "a1" } };
var HISTORY = { items: [], total: 0 };
var POLL = {};
var SH_api = { keys: new Proxy({}, { get: function (t, name) { return function (sid) { return String(name) + ":" + sid; }; } }) };
window.SD_GraphRunView = function () { return null; };
window.primerApi = Object.assign(window.primerApi || {}, {
  apiFetch: function () { return new Promise(function () {}); },
  useFocusTrap: function () {}, focusablesOf: function () { return []; }, useEscape: function () {},
  useViewport: function () { return { isMobile: false }; },
  useResource: function (key, fetcher, opts) {
    POLL[key] = opts && opts.pollMs;
    var data = key === "session:s1" ? SESSION_ROW : key === "session:s1:messages" ? HISTORY : null;
    return { data: data, error: null, loading: false, refetch: function () {} };
  },
});
function frame(ev) { OPENED[0].onmessage({ data: JSON.stringify(ev) }); }
function commit() { var s = SS_getStore("w1", "s1"); s._forceCommit = true; SS_flushTranscript(s); }
function ts(s) { return new Date(Date.UTC(2026, 9, 31, 10, 0, 0) + s * 1000).toISOString(); }
for (var i = 1; i <= 50; i++) {
  HISTORY.items.push({ seq: 2 * i - 1, kind: "user_input", ts: ts(2 * i - 1), payload: { text: "ask " + i } });
  HISTORY.items.push({ seq: 2 * i, kind: "assistant_token", ts: ts(2 * i), payload: { text: "answer " + i } });
}
HISTORY.total = HISTORY.items.length;
"""


@pytest.fixture
def doc() -> Iterator[object]:
    prelude = "var window = globalThis;\n" + "\n;\n".join([
        _src("foundation/shell-status.js"),
        _src("foundation/shell-turns.js"),
        _src("foundation/session-store.js"),
        _src("foundation/use-workspace-tap.js"),
        _src("foundation/shell-attention.js"),
        _src("foundation/shell-verbs.js"),
        transpile(UI / "components" / "session-adapter.jsx"),
        STUBS,
    ])
    c = mini_react_context(transpile(DOC_PATH), prelude)
    c.eval('MR.mount(NV_SessionDoc, { sid: "s1" }); commit(); MR.settle();')
    try:
        yield c
    finally:
        c.close()


def js(c, expr: str):
    return json.loads(c.eval("JSON.stringify((function () { return " + expr + "; })())"))


def other_session_streams(c, n: int) -> None:
    c.eval(
        "frame({ 'class': 'part_start', session_id: 'other', part_id: 'o:text', kind: 'text' });"
        " for (var k = 0; k < " + str(n) + "; k++) frame({ 'class': 'text_delta', session_id: 'other', part_id: 'o:text', kind: 'text', delta: 'tok ' });"
    )


# ---- characterization ---------------------------------------------------------------------------------------------------


def test_the_transcript_draws_every_answer_in_order(doc) -> None:
    answers = [t for t in js(doc, "MR.texts()") if t.startswith("answer ")]
    assert answers == ["answer " + str(i) for i in range(1, 51)]
    assert js(doc, "MD") >= 50


def test_the_status_strip_follows_this_sessions_frames(doc) -> None:
    doc.eval("frame({ 'class': 'user_input', session_id: 's1', seq: 101, ts: ts(101), payload: { text: 'go' } }); MR.settle();")
    assert "thinking" in js(doc, "(MR.find('nv-status-live') || {}).props.children")
    doc.eval("frame({ 'class': 'tool_call', session_id: 's1', seq: 102, ts: ts(102), payload: { name: 'shell', id: 'c1' } }); MR.settle();")
    assert "shell" in js(doc, "(MR.find('nv-status-live') || {}).props.children")
    doc.eval("frame({ 'class': 'done', session_id: 's1', seq: 103, ts: ts(103), payload: {} }); MR.settle();")
    assert js(doc, "MR.find('nv-status-live')") is None


def test_an_ended_session_polls_again_once_the_tap_shows_its_turn(doc) -> None:
    """``live`` lifts the ended-session poll stop: a reopened session is running before its polled row says so."""
    # The poll gate reads terminalRef, which the render after the row's sets: two renders, as two polls would give.
    doc.eval("SESSION_ROW = Object.assign({}, SESSION_ROW, { status: 'ended' }); MR.rerender(); MR.rerender();")
    assert js(doc, "POLL['session:s1']") == 0
    doc.eval("frame({ 'class': 'tool_call', session_id: 's1', seq: 102, ts: ts(102), payload: { name: 'shell', id: 'c1' } }); MR.settle();")
    assert js(doc, "POLL['session:s1']") == 2000


# ---- UI-M1 ----------------------------------------------------------------------------------------------------------------


def test_another_sessions_stream_does_not_render_the_document(doc) -> None:
    before = js(doc, "MD")
    other_session_streams(doc, 20)
    assert js(doc, "MR.settle()") is False, "no state of the document changed"
    assert js(doc, "MD") == before


def test_this_sessions_stream_parses_only_the_row_that_grew(doc) -> None:
    doc.eval(
        "frame({ 'class': 'part_start', session_id: 's1', part_id: 'n:text', kind: 'text' });"
        " frame({ 'class': 'text_delta', session_id: 's1', part_id: 'n:text', kind: 'text', delta: 'Hel' }); commit(); MR.settle();"
    )
    before = js(doc, "MD")
    doc.eval("frame({ 'class': 'text_delta', session_id: 's1', part_id: 'n:text', kind: 'text', delta: 'lo' }); commit(); MR.settle();")
    assert js(doc, "MD") - before == 1
    assert "Hello" in js(doc, "MR.texts()")


def test_the_document_has_no_render_hook_on_the_tap() -> None:
    assert "useWorkspaceTap(" not in DOC
