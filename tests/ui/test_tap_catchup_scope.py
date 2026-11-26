"""UI-H1: a tap (re)open catches up what is shown, not the whole history of every listed session.

Every session the rail lists draws a pulse dot, and each dot binds a session store (``useSessionStore``). The tap hub
(``ui/foundation/use-workspace-tap.js``) used to give EVERY store of the workspace the full REST catch-up on each
``onopen``: from seq 0 for a dot's store, up to 10 pages of 1000 records, all kept in memory. A rail of 200 sessions
issued hundreds of reads per (re)connect and named every session in the next ``?cursor=``. Now:

* only a store with a transcript subscriber (an open session document) gets the full catch-up;
* a store that only feeds a dot gets one tail read (``?tail=1&limit=50``), applied to its status alone: the dot shows
  the same verb and no record is kept;
* ``useSessionStore`` counts a consumer once while it is mounted (it retained on every render, so ``refs`` only grew).

MiniRacer over the real ``session-store.js`` and ``use-workspace-tap.js`` with a fake ``EventSource`` and a counting
``apiFetch`` (the house bare-context style); the hook runs on the ``tests/ui/_mini_react.py`` runtime.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from tests.ui._mini_react import ROOT, mini_react_context

STORE_SRC = (ROOT / "ui" / "foundation" / "session-store.js").read_text(encoding="utf-8")
TAP_SRC = (ROOT / "ui" / "foundation" / "use-workspace-tap.js").read_text(encoding="utf-8")

# Every session on the fake server has LAST records. Every hundredth opens a turn (user_input at seq 1, 101, ...), seq
# 99, 199, ... is a shell call, the rest is answer text; so the newest status-bearing record is the shell call at 2499.
SERVER = r"""
var window = globalThis;
window.setTimeout = undefined; window.requestAnimationFrame = undefined;
window.btoa = function (s) { return s; };
var OPENED = [];
function EventSource(url) { this.url = url; this.readyState = 0; this.closed = false; OPENED.push(this); }
EventSource.prototype.close = function () { this.closed = true; this.readyState = 2; };
var LAST = 2500;
var CALLS = [];
function record(s) {
  var ts = new Date(Date.UTC(2026, 9, 31, 10, 0, 0) + s * 1000).toISOString();
  if (s % 100 === 1) return { seq: s, kind: "user_input", ts: ts, payload: { text: "ask " + s } };
  if (s % 100 === 99) return { seq: s, kind: "tool_call", ts: ts, payload: { name: "shell", id: "c" + s } };
  return { seq: s, kind: "assistant_token", ts: ts, payload: { text: "answer " + s } };
}
window.primerApi = { apiFetch: function (m, url) {
  CALLS.push(url);
  var limit = +/limit=(\d+)/.exec(url)[1];
  var lo, hi;
  if (/[?&]tail=1/.test(url)) { hi = LAST; lo = Math.max(1, LAST - limit + 1); }
  else { var after = +/after_seq=(\d+)/.exec(url)[1]; lo = after + 1; hi = Math.min(after + limit, LAST); }
  var items = [];
  for (var s = lo; s <= hi; s++) items.push(record(s));
  return Promise.resolve({ items: items });
} };
function seqsOf(store) { return store.recordsBySeq.map(function (r) { return r.seq; }); }
function cursorSeqs(url) {
  var m = /cursor=([^&]+)/.exec(url);
  return m ? JSON.parse(decodeURIComponent(m[1])).seqs : {};
}
"""


@pytest.fixture
def ctx() -> Iterator[object]:
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    c.eval(SERVER)
    c.eval(STORE_SRC)
    c.eval(TAP_SRC)
    try:
        yield c
    finally:
        c.close()


def js(c, expr: str):
    return json.loads(c.eval("JSON.stringify((function () { return " + expr + "; })())"))


def open_session(c, sid: str) -> None:
    """A store an open session document binds: it has a transcript subscriber."""
    c.eval("SS_subscribe(SS_getStore('w1', " + json.dumps(sid) + "), 'transcript', function () {});")


def dots(c, n: int) -> None:
    """``n`` stores only a pulse dot reads (status channel only)."""
    c.eval("for (var i = 0; i < " + str(n) + "; i++) SS_subscribe(SS_getStore('w1', 'dot-' + i), 'status', function () {});")


def tap_opens(c) -> None:
    c.eval("WTAP_retain('w1'); OPENED[OPENED.length - 1].onopen();")


# ---- characterization: what the open document and the dot showed before, and still show ----------------------------


def test_an_open_session_gets_its_whole_gap_in_seq_order(ctx) -> None:
    open_session(ctx, "open")
    ctx.eval("var st = SS_getStore('w1', 'open'); for (var s = 1; s <= 5; s++) SS_apply(st, record(s));")
    tap_opens(ctx)
    assert js(ctx, "CALLS") == [
        "/sessions/open/messages?after_seq=5&limit=1000",
        "/sessions/open/messages?after_seq=1005&limit=1000",
        "/sessions/open/messages?after_seq=2005&limit=1000",
    ]
    assert js(ctx, "seqsOf(SS_getStore('w1', 'open'))") == list(range(1, 2501))
    rows = js(ctx, "SS_getSnapshot(SS_getStore('w1', 'open'), 'transcript').map(function (r) { return [r.seq, r.text]; })")
    assert rows[0] == [1, "ask 1"] and rows[1] == [2, "answer 2"] and rows[-1] == [2500, "answer 2500"]


def test_a_dot_shows_the_verb_and_start_of_the_newest_call(ctx) -> None:
    dots(ctx, 1)
    tap_opens(ctx)
    status = js(ctx, "SS_getSnapshot(SS_getStore('w1', 'dot-0'), 'status')")
    assert status["verb"] == "shell"
    assert status["startedMs"] == js(ctx, "Date.parse(record(2499).ts)")


def test_a_dot_of_a_finished_session_shows_nothing(ctx) -> None:
    ctx.eval("var plain = record; record = function (s) { return s === LAST ? { seq: s, kind: 'done', payload: {} } : plain(s); };")
    dots(ctx, 1)
    tap_opens(ctx)
    assert js(ctx, "SS_getStore('w1', 'dot-0').status") is None


def test_the_next_cursor_names_the_open_session_at_its_high_water(ctx) -> None:
    open_session(ctx, "open")
    tap_opens(ctx)
    ctx.eval("WTAP_release('w1'); WTAP_retain('w1');")
    assert js(ctx, "cursorSeqs(OPENED[1].url)")["open"] == 2500


# ---- the fix ----------------------------------------------------------------------------------------------------------


def test_one_open_reads_the_whole_gap_only_for_the_open_session(ctx) -> None:
    """The probe's storm, cut down: 50 dot-only stores and one open document. One full catch-up (three pages for 2500
    records), at most one 50-record tail read per dot, and no dot's store keeps the records."""
    dots(ctx, 50)
    open_session(ctx, "open")
    tap_opens(ctx)
    calls = js(ctx, "CALLS")
    full = [u for u in calls if "after_seq=" in u]
    tails = [u for u in calls if "tail=1" in u]
    assert full == [
        "/sessions/open/messages?after_seq=0&limit=1000",
        "/sessions/open/messages?after_seq=1000&limit=1000",
        "/sessions/open/messages?after_seq=2000&limit=1000",
    ], "only the open session is read in full"
    assert len(tails) <= 50 and all(u.endswith("/messages?tail=1&limit=50") for u in tails), tails
    assert len(calls) == len(full) + len(tails)
    held = js(ctx, "(function () { var m = 0; for (var i = 0; i < 50; i++) m = Math.max(m, SS_getStore('w1', 'dot-' + i).recordsBySeq.length); return m; })()")
    assert held <= 50, "a dot's store keeps no history"
    verbs = js(ctx, "(function () { var v = {}; for (var i = 0; i < 50; i++) { var s = SS_getStore('w1', 'dot-' + i).status; v[s && s.verb] = 1; } return Object.keys(v); })()")
    assert verbs == ["shell"], "every dot still shows the running call"


def test_the_next_cursor_does_not_name_sessions_only_a_dot_shows(ctx) -> None:
    dots(ctx, 50)
    open_session(ctx, "open")
    tap_opens(ctx)
    ctx.eval("WTAP_release('w1'); WTAP_retain('w1');")
    assert js(ctx, "cursorSeqs(OPENED[1].url)") == {"open": 2500}


# ---- the hook counts a consumer once ----------------------------------------------------------------------------------

DOT = r"""
function Dot() {
  var st = window.useSessionStore("w1", "s1", "status");
  return React.createElement("i", { "data-testid": "dot" }, st ? st.verb : "-");
}
function Nothing() { return null; }
"""


@pytest.fixture
def hook_ctx() -> Iterator[object]:
    c = mini_react_context(STORE_SRC + "\n;\n" + DOT, "var window = globalThis; window.setTimeout = undefined; window.requestAnimationFrame = undefined;")
    try:
        yield c
    finally:
        c.close()


def test_a_mounted_dot_is_one_reference_however_often_it_renders(hook_ctx) -> None:
    hook_ctx.eval("MR.mount(Dot, {}); MR.rerender(); MR.rerender(); MR.rerender();")
    assert js(hook_ctx, "SS_getStore('w1', 's1').refs") == 1
    hook_ctx.eval("MR.mount(Nothing, {});")
    assert js(hook_ctx, "SS_getStore('w1', 's1').refs") == 0, "unmount releases it"


def test_a_mounted_dot_follows_the_status(hook_ctx) -> None:
    hook_ctx.eval("MR.mount(Dot, {});")
    hook_ctx.eval("SS_apply(SS_getStore('w1', 's1'), { session_id: 's1', seq: 1, kind: 'tool_call', payload: { name: 'grep' } }); MR.rerender();")
    assert js(hook_ctx, "MR.texts()") == ["grep"]


# ---- a freed session does not grow again (lead ruling on the UI-H1 gap) ------------------------------------------------


def test_a_freed_session_takes_only_status_from_frames_and_reloads_its_history_when_shown(ctx) -> None:
    """Once nothing displays a session and its records were freed (60 s after the last release), a live frame moves only its
    status, its pending send and its high-water: no record, no live part. Shown again, the normal catch-up loads its
    history, as on first open."""
    ctx.eval(
        "LAST = 55; var plain = record;"
        " record = function (s) { return s === LAST ? { seq: s, kind: 'tool_call', ts: plain(s).ts, payload: { name: 'grep', id: 'g' } } : plain(s); };"
        " var TIMERS = [];"
        " window.setTimeout = function (fn, ms) { TIMERS.push({ fn: fn, ms: ms }); return TIMERS.length; }; window.clearTimeout = function () {};"
        " var gone = SS_getStore('w1', 'gone'); SS_retain(gone);"
        " var unsub = SS_subscribe(gone, 'transcript', function () {});"
        " for (var s = 1; s <= 5; s++) SS_apply(gone, Object.assign({ session_id: 'gone' }, record(s)));"
        " unsub(); SS_dispose(gone); TIMERS.splice(0).filter(function (t) { return t.ms === SS_FREE_AFTER_MS; }).forEach(function (t) { t.fn(); });"
    )
    assert js(ctx, "gone.recordsBySeq.length") == 0, "freed"
    ctx.eval(
        "WTAP_retain('w1'); var es = OPENED[0];"
        " function tapFrame(f) { es.onmessage({ data: JSON.stringify(f) }); }"
        " tapFrame({ 'class': 'part_start', session_id: 'gone', part_id: 'n:text', kind: 'text' });"
        " for (var s = 6; s <= 55; s++) {"
        "   var r = record(s);"
        "   tapFrame({ 'class': r.kind, session_id: 'gone', seq: r.seq, ts: r.ts, payload: r.payload });"
        "   tapFrame({ 'class': 'text_delta', session_id: 'gone', part_id: 'n:text', kind: 'text', delta: 'tok ' });"
        " }"
    )
    assert js(ctx, "gone.recordsBySeq.length") == 0 and js(ctx, "Object.keys(gone.records).length") == 0
    assert js(ctx, "Object.keys(gone.parts)") == []
    status = js(ctx, "SS_getSnapshot(gone, 'status')")
    assert status["verb"] == "grep" and status["startedMs"] == js(ctx, "Date.parse(record(55).ts)")
    assert js(ctx, "SS_highWater(gone)") == 55
    # Shown again (an open document binds it), the hub's catch-up on (re)open loads the history from the start.
    ctx.eval("SS_retain(gone); SS_subscribe(gone, 'transcript', function () {}); CALLS = []; es.onopen();")
    assert js(ctx, "CALLS") == ["/sessions/gone/messages?after_seq=0&limit=1000"]
    assert js(ctx, "seqsOf(gone)") == list(range(1, 56))
    assert js(ctx, "SS_getStore('w1', 'gone').status.verb") == "grep"


# ---- a pulse dot is a status-only consumer (lead ruling, the last UI-H1 step) --------------------------------------------
# The rail's real NV_Rail_SessionPulse, on the mini React runtime with the real store and hub; DocView stands in for an open
# session document (a transcript consumer).

RAIL_EXTRA = r"""
function DocView() { window.useSessionStore("w1", "s1", "transcript"); return null; }
function DotAndDoc() {
  return [React.createElement(NV_Rail_SessionPulse, { key: "dot", wid: "w1", sid: "s1" }), React.createElement(DocView, { key: "doc" })];
}
function tapFrames(lo, hi) {
  for (var s = lo; s <= hi; s++) {
    var r = record(s);
    OPENED[0].onmessage({ data: JSON.stringify({ "class": r.kind, session_id: "s1", seq: r.seq, ts: r.ts, payload: r.payload }) });
    OPENED[0].onmessage({ data: JSON.stringify({ "class": "text_delta", session_id: "s1", part_id: "n:text", kind: "text", delta: "tok " }) });
  }
  MR.settle();
}
"""


@pytest.fixture
def rail_ctx() -> Iterator[object]:
    from tests.ui._mini_react import transpile

    code = "\n;\n".join([transpile(ROOT / "ui" / "components" / "console" / "nv-rail.jsx"), RAIL_EXTRA])
    c = mini_react_context(code, SERVER + STORE_SRC + "\n;\n" + TAP_SRC)
    c.eval(
        "LAST = 50; var plain = record;"
        " record = function (s) { return s === 50 ? { seq: s, kind: 'tool_call', ts: plain(s).ts, payload: { name: 'grep', id: 'g' } } : plain(s); };"
        " WTAP_retain('w1');"
    )
    try:
        yield c
    finally:
        c.close()


def test_a_pulse_dot_keeps_no_records_and_shows_the_current_status(rail_ctx) -> None:
    rail_ctx.eval("MR.mount(NV_Rail_SessionPulse, { wid: 'w1', sid: 's1' }); tapFrames(1, 50);")
    store = "SS_getStore('w1', 's1')"
    assert js(rail_ctx, store + ".recordsBySeq.length") == 0 and js(rail_ctx, "Object.keys(" + store + ".parts)") == []
    status = js(rail_ctx, "SS_getSnapshot(" + store + ", 'status')")
    assert status["verb"] == "grep" and status["startedMs"] == js(rail_ctx, "Date.parse(record(50).ts)")
    assert js(rail_ctx, "MR.findProp('className', 'nv-dot-pulse') !== null") is True


def test_a_document_mounting_beside_the_dot_gets_the_history_and_the_frames_again(rail_ctx) -> None:
    rail_ctx.eval("MR.mount(NV_Rail_SessionPulse, { wid: 'w1', sid: 's1' }); tapFrames(1, 50);")
    rail_ctx.eval("MR.mount(DotAndDoc, {}); CALLS = []; OPENED[0].onopen();")
    assert js(rail_ctx, "CALLS") == ["/sessions/s1/messages?after_seq=0&limit=1000"], "the catch-up reads the history from the start"
    rail_ctx.eval("LAST = 55; tapFrames(51, 55);")
    assert js(rail_ctx, "seqsOf(SS_getStore('w1', 's1'))") == list(range(1, 56))


def test_every_pulse_dot_declares_it_needs_no_records() -> None:
    for rel in ("nv-rail.jsx", "nv-mobile-shell.jsx", "nv-tab-groups.jsx"):
        src = (ROOT / "ui" / "components" / "console" / rel).read_text(encoding="utf-8")
        assert '"status", { records: false })' in src, rel


# ---- review 8.5: stores are evicted, a reconnect catches up only held stores, a few at a time, and the cursor stays bounded -----------


def _hold(c, sid: str, channel: str = "status") -> None:
    c.eval("SS_retain(SS_getStore('w1', " + json.dumps(sid) + "), { records: " + ("true" if channel == "transcript" else "false") + " });")


def test_a_store_nobody_holds_is_not_caught_up_and_not_in_the_cursor(ctx) -> None:
    """A session the console once showed (store kept, every consumer gone) is not read again on a reconnect and does not lengthen the cursor."""
    dots(ctx, 2)
    ctx.eval("var gone = SS_getStore('w1', 'gone'); SS_retain(gone); SS_apply(gone, record(1)); SS_dispose(gone);")
    ctx.eval("SS_apply(SS_getStore('w1', 'dot-0'), record(2));")
    tap_opens(ctx)
    assert not any("/sessions/gone/" in u for u in js(ctx, "CALLS")), "no catch-up for a store nobody holds"
    seqs = js(ctx, "cursorSeqs(WTAP_cursorUrl(WTAP_getHub('w1')))")
    assert "gone" not in seqs and "dot-0" in seqs


def test_a_reconnect_reads_a_few_stores_at_a_time(ctx) -> None:
    ctx.eval("window.React = {};")
    ctx.eval((ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8"))
    ctx.eval(
        "var LIVE = 0, PEAK = 0, DONE = 0, PENDING = [];"
        "window.primerApi.apiFetch = function () { LIVE += 1; PEAK = Math.max(PEAK, LIVE);"
        " return new Promise(function (res) { PENDING.push(function () { LIVE -= 1; DONE += 1; res({ items: [] }); }); }); };"
    )
    dots(ctx, 30)
    tap_opens(ctx)
    assert js(ctx, "PEAK") == 4 and js(ctx, "PENDING.length") == 4
    for _ in range(60):
        ctx.eval("var f = PENDING.shift(); if (f) f();")
    assert js(ctx, "PEAK") == 4, "never more than four in flight"
    assert js(ctx, "DONE") == 30 and js(ctx, "LIVE") == 0, "every store was read, and the reads all finished"


def test_the_resume_cursor_names_at_most_fifty_sessions_open_documents_first(ctx) -> None:
    dots(ctx, 80)
    ctx.eval("for (var i = 0; i < 80; i++) SS_apply(SS_getStore('w1', 'dot-' + i), record(1000 + i));")
    open_session(ctx, "open")
    ctx.eval("SS_apply(SS_getStore('w1', 'open'), record(1)); SS_retain(SS_getStore('w1', 'open'));")
    ctx.eval("window.primerApi.apiFetch = function () { return new Promise(function () {}); };")
    ctx.eval("WTAP_retain('w1');")
    seqs = js(ctx, "cursorSeqs(OPENED[0].url)")
    assert len(seqs) == 50
    assert "open" in seqs, "a session an open document reads is always named"


def test_a_store_every_consumer_left_is_evicted_after_the_grace_and_a_held_one_is_not(ctx) -> None:
    ctx.eval(
        "var TIMERS = [];"
        "window.setTimeout = function (fn, ms) { TIMERS.push({ fn: fn, ms: ms, live: true }); return TIMERS.length; };"
        "window.clearTimeout = function (id) { if (TIMERS[id - 1]) TIMERS[id - 1].live = false; };"
        "var gone = SS_getStore('w1', 'gone'); SS_retain(gone); SS_dispose(gone);"
        "var held = SS_getStore('w1', 'held'); SS_retain(held);"
        "var back = SS_getStore('w1', 'back'); SS_retain(back); SS_dispose(back); SS_retain(back);"
        "var never = SS_getStore('w1', 'never');"   # created, never held: evicted too
    )
    assert js(ctx, "Object.keys(SS_STORES).length") == 4
    ctx.eval("TIMERS.forEach(function (t) { if (t.live && t.ms === SS_EVICT_AFTER_MS) t.fn(); });")
    assert sorted(k.split(":", 2)[2] for k in js(ctx, "Object.keys(SS_STORES)")) == ["back", "held"]
    # A session shown again after its eviction starts a fresh store.
    assert js(ctx, "SS_getStore('w1', 'gone') !== gone") is True
