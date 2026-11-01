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


# ---- UI-M5: the transcript follows a streaming answer ---------------------------------------------------------------------
# The harness has no DOM, so the transcript's scroll box is a stand-in object handed to its ref.

def pane(c, *, scroll_top: int) -> None:
    c.eval(
        "var PANE = { scrollTop: " + str(scroll_top) + ", scrollHeight: 5000, clientHeight: 500 };"
        " MR.find('nv-transcript').props.ref.current = PANE;"
        " MR.find('nv-transcript').props.onScroll(); MR.settle();"
    )


def stream_into_this_session(c, text: str) -> None:
    c.eval(
        "frame({ 'class': 'part_start', session_id: 's1', part_id: 'n:text', kind: 'text' });"
        " frame({ 'class': 'text_delta', session_id: 's1', part_id: 'n:text', kind: 'text', delta: " + json.dumps(text) + " });"
        " commit(); MR.settle();"
    )


def test_a_new_row_scrolls_a_following_transcript_to_the_bottom(doc) -> None:
    pane(doc, scroll_top=4500)  # at the bottom: following
    doc.eval("PANE.scrollHeight = 6000; frame({ 'class': 'user_input', session_id: 's1', seq: 101, ts: ts(101), payload: { text: 'next' } }); commit(); MR.settle();")
    assert js(doc, "PANE.scrollTop") == 6000


def test_a_transcript_scrolled_up_is_left_where_it_is(doc) -> None:
    pane(doc, scroll_top=1000)  # read further up: not following
    doc.eval("PANE.scrollHeight = 6000;")
    stream_into_this_session(doc, "a long answer")
    doc.eval("frame({ 'class': 'user_input', session_id: 's1', seq: 101, ts: ts(101), payload: { text: 'next' } }); commit(); MR.settle();")
    assert js(doc, "PANE.scrollTop") == 1000


def test_a_following_transcript_follows_the_answer_as_it_grows(doc) -> None:
    """The live part is one row whose text grows: the row count does not move, so the follow effect never ran."""
    stream_into_this_session(doc, "Hel")
    pane(doc, scroll_top=4500)
    doc.eval("PANE.scrollHeight = 5200;")
    doc.eval("frame({ 'class': 'text_delta', session_id: 's1', part_id: 'n:text', kind: 'text', delta: 'lo, and more' }); commit(); MR.settle();")
    assert js(doc, "PANE.scrollTop") == 5200


def test_the_follow_effect_depends_on_the_transcript_snapshot() -> None:
    effect = DOC[DOC.index("if (follow) jumpLatest();"):]
    deps = effect[effect.index("}, ["):effect.index("]);")]
    assert "transcriptSnap" in deps


# ---- UI-M6: push-to-talk released before the microphone opens -----------------------------------------------------------
# The composer alone, with a getUserMedia the test resolves by hand, a MediaRecorder stand-in, and timers it fires itself.

MIC = r"""
var GUM = [], TRACKS = [], RECORDERS = [], TIMERS = [], POSTS = [];
var navigator = { mediaDevices: { getUserMedia: function () {
  return new Promise(function (resolve) { GUM.push(resolve); });
} } };
function micStream() {
  var t = { stopped: false, stop: function () { this.stopped = true; } };
  TRACKS.push(t);
  return { getTracks: function () { return [t]; } };
}
function MediaRecorder(stream) { this.stream = stream; this.state = "inactive"; this.started = 0; RECORDERS.push(this); }
MediaRecorder.prototype.start = function () { this.state = "recording"; this.started += 1; };
MediaRecorder.prototype.stop = function () { this.state = "inactive"; if (this.onstop) this.onstop(); };
function Blob() {}
function FormData() { this.append = function () {}; }
setTimeout = function (fn) { TIMERS.push({ fn: fn, id: TIMERS.length + 1, live: true }); return TIMERS.length; };
clearTimeout = function (id) { TIMERS.forEach(function (t) { if (t.id === id) t.live = false; }); };
function fireTimers() { TIMERS.splice(0).forEach(function (t) { if (t.live) t.fn(); }); MR.settle(); }
function mic(handler) { MR.find("nv-mic").props[handler](); MR.settle(); }
window.primerApi.apiFetch = function (m, path) { POSTS.push(path); return Promise.resolve({ text: "hello there" }); };
"""


@pytest.fixture
def composer() -> Iterator[object]:
    prelude = "var window = globalThis;\n" + "\n;\n".join([
        _src("foundation/shell-status.js"),
        _src("foundation/shell-turns.js"),
        _src("foundation/session-store.js"),
        _src("foundation/use-workspace-tap.js"),
        _src("foundation/shell-attention.js"),
        _src("foundation/shell-verbs.js"),
        transpile(UI / "components" / "session-adapter.jsx"),
        STUBS,
        MIC,
    ])
    c = mini_react_context(transpile(DOC_PATH), prelude)
    c.eval('MR.mount(NV_Composer, { sid: "s1", micEnabled: true, onSendStarted: function () {} });')
    try:
        yield c
    finally:
        c.close()


def opens(c) -> None:
    """The oldest pending getUserMedia answers; its then-callback runs once the eval's microtasks drain, then React renders."""
    c.eval("GUM.shift()(micStream());")
    c.eval("MR.settle();")


def test_hold_to_talk_records_and_lands_the_words_as_editable_text(composer) -> None:
    composer.eval("mic('onMouseDown');")
    opens(composer)
    assert js(composer, "RECORDERS.map(function (r) { return r.state; })") == ["recording"]
    composer.eval("mic('onMouseUp'); fireTimers();")
    assert js(composer, "RECORDERS[0].state") == "inactive"
    assert js(composer, "TRACKS.every(function (t) { return t.stopped; })") is True
    assert js(composer, "POSTS") == ["/audio/transcriptions"]
    composer.eval("MR.settle();")
    assert js(composer, "MR.find('nv-composer-input').props.value") == "hello there"


def test_a_release_before_the_microphone_opens_never_starts_recording(composer) -> None:
    """The permission prompt is up while the button is held; the operator lets go, then allows. The stop ran first and found
    nothing to stop; the recorder then started and kept recording until the next press."""
    composer.eval("mic('onMouseDown'); mic('onMouseUp'); fireTimers();")
    opens(composer)
    assert js(composer, "RECORDERS.filter(function (r) { return r.started; }).length") == 0
    assert js(composer, "TRACKS.length") == 1 and js(composer, "TRACKS[0].stopped") is True


def test_a_double_tap_before_the_microphone_opens_leaves_one_recorder_to_stop(composer) -> None:
    """Two presses before getUserMedia answers asked for two streams; the first recorder was orphaned with its track live."""
    composer.eval("mic('onMouseDown'); mic('onMouseUp'); mic('onMouseDown'); mic('onMouseUp');")  # a double tap: latched
    opens(composer)
    opens(composer)
    assert js(composer, "RECORDERS.filter(function (r) { return r.started; }).length") == 1
    composer.eval("mic('onMouseDown'); mic('onMouseUp');")  # a press while latched stops it
    assert js(composer, "RECORDERS.every(function (r) { return r.state === 'inactive'; })") is True
    assert js(composer, "TRACKS.every(function (t) { return t.stopped; })") is True
