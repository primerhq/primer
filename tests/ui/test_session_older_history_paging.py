"""G9-2: the session transcript's older-history paging (fix PR A).

The session document's history read is the NEWEST 200 records of the session's
span (``SH_api.messages`` -> ``GET /sessions/{sid}/messages?limit=200&tail=1``),
and the endpoint pages older rows from that tail: with ``tail``, ``offset`` counts
rows from the end (``primer/api/routers/sessions.py``, ``_read_workspace_turn_log``),
and the response carries the span's ``total``. Three surfaces make a session with
more than 200 transcript records show its older history instead of a silently
truncated conversation:

* ``SH_api.messages`` takes a ``tailOffset`` and builds ``&offset=<n>`` for it
  (``ui/components/shell/sh-api.jsx``) - the old ``beforeSeq`` parameter was
  ignored entirely;
* the top-of-transcript seam ``NV_LoadOlderSeam`` (``ui/components/console/nv-session-doc.jsx``)
  draws only while the span's ``total`` exceeds the rendered records (the tail
  window plus the pages the seam has loaded), reads exactly one older page per
  click, and PREPENDS the page through the store, so the older records land
  seq-ordered ahead of the newer ones;
* ``SS_catchUp`` (``ui/foundation/session-store.js``) loops the catch-up read
  while a page comes back full, so a tap gap over one page (1000 records) is
  fully caught instead of silently truncated until a reload.

The harness is the house one: MiniRacer over the real source - the seam runs on
the ``tests/ui/_mini_react.py`` hook runtime with the real store and the real
``SH_api``; the URL builder and the catch-up loop run in a bare context.
``tests/ui_e2e/test_session_older_history_journey.py`` is the journey (lead lane).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.ui._mini_react import ROOT, mini_react_context, transpile

UI = ROOT / "ui"
DOC_PATH = UI / "components" / "console" / "nv-session-doc.jsx"
DOC = DOC_PATH.read_text(encoding="utf-8")
SH_API_SRC = (UI / "components" / "shell" / "sh-api.jsx").read_text(encoding="utf-8")
STORE_SRC = (UI / "foundation" / "session-store.js").read_text(encoding="utf-8")

TALKING = "Load older messages"


@pytest.fixture
def api_ctx() -> Iterator[object]:
    """A bare context with the real ``SH_api`` and a recording ``apiFetch``."""
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    c.eval("var window = globalThis;")
    c.eval("var CALLS = [];")
    c.eval(
        "window.primerApi = { apiFetch: function (m, path) { CALLS.push([m, path]); return Promise.resolve({}); } };"
    )
    c.eval(SH_API_SRC)  # plain JS: loads as written
    try:
        yield c
    finally:
        c.close()


def js(ctx, expr: str):
    """The value of a JS expression as Python data; ``undefined`` is ``None``, as ``null`` is."""
    return json.loads(ctx.eval(f"(function () {{ var v = ({expr}); return JSON.stringify(v === undefined ? null : v); }})()"))


# ---- (a) the URL builder --------------------------------------------------------


def test_messages_builds_the_tail_window_and_older_tail_offsets(api_ctx) -> None:
    """A null or 0 offset sends the plain tail window; a nonzero ``tailOffset`` is the endpoint's
    ``offset`` (rows from the end), so the next older page of a transcript that renders ``n``
    records is ``?limit=200&tail=1&offset=n``."""
    api_ctx.eval("SH_api.messages('s1', 200, null, null)")
    api_ctx.eval("SH_api.messages('s1', 200, 0, null)")
    api_ctx.eval("SH_api.messages('s1', 200, 400, null)")
    assert json.loads(api_ctx.eval("JSON.stringify(CALLS)")) == [
        ["GET", "/sessions/s1/messages?limit=200&tail=1"],
        ["GET", "/sessions/s1/messages?limit=200&tail=1"],
        ["GET", "/sessions/s1/messages?limit=200&tail=1&offset=400"],
    ]


def test_the_history_read_call_site_still_passes_the_tail_window() -> None:
    """The one caller reads the plain tail window (third argument null): a renamed or dropped parameter
    must not change what that call sends."""
    assert "SH_api.messages(sid, 200, null, signal)" in DOC, "the history read keeps the tail-window shape"


# ---- (b) and (d) the seam -------------------------------------------------------

# The prelude stubs the console the seam sits in: a recording ``apiFetch`` answered from ``ANSWERS``
# (the real ``SH_api`` builds the URL), and the scheduler-free store commit path.
SEAM_PRELUDE = r"""
var window = globalThis;
window.setTimeout = undefined; window.requestAnimationFrame = undefined;
var CALLS = [], ANSWERS = [], REJECT = null;
window.primerApi = {
  apiFetch: function (m, path) {
    CALLS.push([m, path]);
    if (REJECT) return Promise.reject(REJECT);
    return Promise.resolve(ANSWERS.shift() || { items: [] });
  }
};
function textOf(node) {
  var out = [];
  (function collect(n) {
    if (n == null || typeof n === "boolean") return;
    if (typeof n === "string" || typeof n === "number") { out.push(String(n)); return; }
    if (Array.isArray(n)) { n.forEach(collect); return; }
    if (n.__el) collect(typeof n.type === "function" && n.type !== React.Fragment ? n.out : n.children);
  })(node);
  return out.join("");
}
"""


@pytest.fixture
def seam_ctx() -> Iterator[object]:
    """The mini React runtime over the real document source, the real ``SH_api`` and the real store."""
    code = "\n;\n".join([transpile(DOC_PATH), SH_API_SRC, STORE_SRC])
    c = mini_react_context(code, SEAM_PRELUDE)
    try:
        yield c
    finally:
        c.close()


def seed_tail_window(ctx, first_seq: int, last_seq: int) -> None:
    """The records the tail window already rendered: seq ``first_seq..last_seq`` in the store."""
    ctx.eval(
        "var store = SS_getStore('w1', 's1');"
        " for (var s = " + str(first_seq) + "; s <= " + str(last_seq) + "; s++)"
        " SS_apply(store, { session_id: 's1', seq: s, kind: 'user_input', payload: { text: 'row-' + s } });"
    )


def store_seqs(ctx) -> list[int]:
    return js(ctx, "SS_getStore('w1', 's1').recordsBySeq.map(function (r) { return r.seq; })")


def mount_seam(ctx, total: int, tail_count: int) -> None:
    ctx.eval(
        "MR.mount(NV_LoadOlderSeam, { sid: 's1', total: " + str(total) + ", tailCount: " + str(tail_count) + ","
        " applyOlder: function (items) {"
        "  for (var i = 0; i < items.length; i++) SS_apply(SS_getStore('w1', 's1'), Object.assign({}, items[i], { session_id: 's1' }));"
        " } });"
    )


def answer_older_page(ctx, first_seq: int, last_seq: int) -> None:
    """Queue the older page the endpoint would answer (seq ``first_seq..last_seq``, in its response
    envelope) and press the seam."""
    ctx.eval(
        "var p = [];"
        " for (var s = " + str(first_seq) + "; s <= " + str(last_seq) + "; s++)"
        " p.push({ session_id: 's1', seq: s, kind: 'user_input', payload: { text: 'old-' + s } });"
        " ANSWERS = [{ items: p, total: 999999, offset: 0, limit: 200 }];"
    )
    ctx.eval('MR.click("sd-load-older")')
    ctx.eval("MR.rerender()")


def test_the_seam_names_the_older_records_and_loads_exactly_one_page(seam_ctx) -> None:
    """A span of 450 records renders 200: the seam says 250 more, one click reads exactly the page at
    offset 200 (the rendered count), and the older records are rendered BEFORE the existing ones -
    the store's seq order is the transcript's order (``SA_toTranscript`` reads ``recordsBySeq``)."""
    seed_tail_window(seam_ctx, 251, 450)
    mount_seam(seam_ctx, 450, 200)
    assert js(seam_ctx, 'textOf(MR.find("sd-load-older"))') == f"{TALKING} (250 more)"
    assert js(seam_ctx, "NV_olderHistoryRemaining(450, 200, 0)") == 250
    assert js(seam_ctx, "NV_olderPageOffset(200, 0)") == 200 and js(seam_ctx, "NV_olderPageOffset(200, 250)") == 450

    answer_older_page(seam_ctx, 1, 250)

    assert js(seam_ctx, "CALLS") == [["GET", "/sessions/s1/messages?limit=200&tail=1&offset=200"]], (
        "one click is exactly one older read, at the tail offset of the rendered count"
    )
    assert store_seqs(seam_ctx) == list(range(1, 451)), "the older records land ahead of the existing ones, in seq order"
    assert js(seam_ctx, '!!MR.find("sd-load-older")') is False, "the span is fully loaded: the seam is gone"
    assert js(seam_ctx, "CALLS.length") == 1, "a fully loaded span reads nothing further"


def test_a_fully_loaded_history_draws_no_seam_and_reads_nothing(seam_ctx) -> None:
    """``total == items.length`` (probe d): the tail window holds the whole span, so there is no seam
    and no read to make - the click has nothing to reach for."""
    seed_tail_window(seam_ctx, 1, 200)
    mount_seam(seam_ctx, 200, 200)
    assert js(seam_ctx, '!!MR.find("sd-load-older")') is False
    assert js(seam_ctx, "CALLS.length") == 0
    assert js(seam_ctx, "NV_olderHistoryRemaining(200, 200, 0)") == 0


def test_the_second_older_page_reads_at_the_growing_offset(seam_ctx) -> None:
    """A 600-record span: after the first page (offset 200, 200 records) the seam says 200 more and the
    next click reads at offset 400, so a very long transcript is paged to the oldest record."""
    seed_tail_window(seam_ctx, 401, 600)
    mount_seam(seam_ctx, 600, 200)
    answer_older_page(seam_ctx, 201, 400)
    assert js(seam_ctx, "CALLS") == [["GET", "/sessions/s1/messages?limit=200&tail=1&offset=200"]]
    assert js(seam_ctx, 'textOf(MR.find("sd-load-older"))') == f"{TALKING} (200 more)"
    answer_older_page(seam_ctx, 1, 200)
    assert js(seam_ctx, "CALLS") == [
        ["GET", "/sessions/s1/messages?limit=200&tail=1&offset=200"],
        ["GET", "/sessions/s1/messages?limit=200&tail=1&offset=400"],
    ], "the second page is anchored past the first, not re-read"
    assert store_seqs(seam_ctx) == list(range(1, 601))
    assert js(seam_ctx, '!!MR.find("sd-load-older")') is False


def test_the_session_doc_wires_the_seam_to_the_top_of_the_transcript() -> None:
    """The seam is DRAWN by the session document (not just defined): at the top of the transcript, ahead
    of the first row, fed from the history resource's ``total`` and its items, and the loaded page is
    applied to the store the transcript renders from."""
    assert "function NV_LoadOlderSeam" in DOC
    inner = DOC[DOC.index('className="nv-transcript-inner"'):]  # the transcript body's div (a class, not a testid)
    seam = inner.index("<NV_LoadOlderSeam")
    rows = inner.index("NV_scopeToNode(rows, nodeFilter)")
    assert seam < rows, "the seam sits at the top of the transcript, ahead of the first row"
    wired = inner[seam:seam + 500]
    assert "total" in wired and "items" in wired and "applyOlder" in wired


# ---- (c) the catch-up loop ------------------------------------------------------


@pytest.fixture
def store_ctx() -> Iterator[object]:
    """A bare context with the real store, no scheduler, and a holding ``apiFetch`` (each read resolves
    only when the test resolves it, so the loop's pages are driven one at a time)."""
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    c.eval("var window = globalThis;")
    c.eval("window.setTimeout = undefined; window.requestAnimationFrame = undefined;")
    c.eval("var CALLS = [], PENDING = [];")
    c.eval(
        "window.primerApi = { apiFetch: function (m, path) {"
        " CALLS.push(path); return new Promise(function (resolve) { PENDING.push(resolve); }); } };"
    )
    c.eval(STORE_SRC)  # plain JS: loads as written
    try:
        yield c
    finally:
        c.close()


def resolve_page(c, first_seq: int, last_seq: int) -> None:
    c.eval(
        "(function () { var p = [];"
        " for (var s = " + str(first_seq) + "; s <= " + str(last_seq) + "; s++)"
        " p.push({ session_id: 's1', seq: s, kind: 'user_input', payload: { text: 'row-' + s } });"
        " PENDING.shift()( { items: p, total: 999999, offset: 0, limit: 1000 } ); })();"
    )


def test_catch_up_pages_a_gap_over_one_full_page(store_ctx) -> None:
    """A tap gap over 1000 records used to be silently truncated until a full reload: the read now
    loops while a page comes back full, from the page's newest ``seq``, so a 1500-record gap lands
    whole, in seq order."""
    store_ctx.eval(
        "var store = SS_getStore('w1', 's1');"
        " for (var s = 1; s <= 5; s++)"
        " SS_apply(store, { session_id: 's1', seq: s, kind: 'user_input', payload: { text: 'row-' + s } });"
    )
    store_ctx.eval("SS_catchUp(SS_getStore('w1', 's1'))")
    assert js(store_ctx, "CALLS") == ["/sessions/s1/messages?after_seq=5&limit=1000"], "the read starts after the high-water seq"

    resolve_page(store_ctx, 6, 1005)  # a FULL page (1000 records)
    assert js(store_ctx, "CALLS") == [
        "/sessions/s1/messages?after_seq=5&limit=1000",
        "/sessions/s1/messages?after_seq=1005&limit=1000",
    ], "a full page triggers the next read, anchored at the page's newest seq"

    resolve_page(store_ctx, 1006, 1505)  # a SHORT page
    assert js(store_ctx, "CALLS.length") == 2, "a short page ends the loop: no third read"
    seqs = js(store_ctx, "SS_getStore('w1', 's1').recordsBySeq.map(function (r) { return r.seq; })")
    assert seqs == list(range(1, 1506)), "the whole gap landed, in seq order"


def test_catch_up_stops_at_the_cap_and_stays_best_effort(store_ctx) -> None:
    """The loop is capped (10 pages / 10000 records): a server that keeps answering full pages cannot
    spin it; and a refused read stays best-effort (stale-while-error) - the flag clears and the next
    catch-up reads again."""
    store_ctx.eval("SS_catchUp(SS_getStore('w1', 's1'))")
    for page_no in range(10):
        resolve_page(store_ctx, page_no * 1000 + 1, page_no * 1000 + 1000)
    assert js(store_ctx, "CALLS.length") == 10, "the cap is ten pages"
    assert js(store_ctx, "SS_getStore('w1', 's1')._catchUpInFlight") is False, "the cap releases the flag"

    store_ctx.eval(
        "window.primerApi.apiFetch = function (m, path) { CALLS.push(path); return Promise.reject(new Error('no tap')); };"
    )
    store_ctx.eval("SS_catchUp(SS_getStore('w1', 's1'))")
    store_ctx.eval("void 0")  # let the rejection's catch and the flag-clear settle
    assert js(store_ctx, "SS_getStore('w1', 's1')._catchUpInFlight") is False
    assert js(store_ctx, "SS_getStore('w1', 's1').recordsBySeq.length") == 10000, "a refused read applies nothing further (the 10 capped pages remain)"
    store_ctx.eval(
        "window.primerApi.apiFetch = function (m, path) { CALLS.push(path); return Promise.resolve({ items: [] }); };"
    )
    store_ctx.eval("SS_catchUp(SS_getStore('w1', 's1'))")
    store_ctx.eval("void 0")
    assert js(store_ctx, "CALLS.length") == 12, "the next catch-up reads again (stale-while-error)"
