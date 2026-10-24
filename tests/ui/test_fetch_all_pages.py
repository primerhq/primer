"""Every page of a list (ticket 01a16dfa): ``PR_fetchAllPages``, ``PR_pagedNote``, ``PR_PagedNote`` and ``PR_usePagedResource`` in
``ui/foundation/api.js``, run in MiniRacer.

A list route answers one page of at most 200 rows (``primer/api/pagination.py`` caps ``?limit``) as ``{kind: "offset", offset, length, total, items}``,
where ``total`` is null when the backend cannot count. About 35 console lists read one page of 200 and stopped, so a 201st workspace, agent or session
was reachable nowhere in the console. The helper reads every page: ``?limit=200&offset=<n * 200>`` with the path's own query after it, until a short page,
the server's ``total``, a full page that adds no new id (a server that ignores the offset), or the page cap. What it read past the cap, or before a page that
failed, is kept and said (``truncated``), never dropped in silence. The fakes answer from ``offset``, as the server does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
API = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")


@pytest.fixture
def ctx():
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    c.eval("var window = globalThis;")
    c.eval(API)
    try:
        yield c
    finally:
        c.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def _run(ctx, answer: str, path: str = "/agents", opts: str = "{}"):
    """PR_fetchAllPages(path) against a fake whose ``answer`` (JS with ``offset`` and ``limit`` in scope) is each page's response or a thrown error."""
    ctx.eval(
        "var URLS = []; var OUT = null; var ERR = null;"
        "function fake(method, path, body, o) { URLS.push(path);"
        "  var offset = Number(/[?&]offset=(\\d+)/.exec(path)[1]); var limit = Number(/[?&]limit=(\\d+)/.exec(path)[1]);"
        "  " + answer + " }"
        f"PR_fetchAllPages({json.dumps(path)}, Object.assign({{ apiFetch: fake }}, {opts}))"
        ".then(function (r) { OUT = r; }, function (e) { ERR = { name: e.name, message: e.message, status: e.status == null ? null : e.status }; });"
    )
    return js(ctx, "OUT"), js(ctx, "URLS"), js(ctx, "ERR")


def _rows(total: int, server_total: str = "null") -> str:
    """A server holding ``total`` rows a-0 .. a-<total-1>, in id order."""
    return (
        f"var n = Math.max(0, Math.min(limit, {total} - offset)); var items = [];"
        " for (var i = 0; i < n; i++) items.push({ id: 'a-' + (offset + i) });"
        f" return Promise.resolve({{ kind: 'offset', offset: offset, length: n, total: {server_total}, items: items }});"
    )


def test_it_reads_every_page_past_200(ctx):
    out, urls, err = _run(ctx, _rows(450))
    assert err is None
    assert urls == ["/agents?limit=200&offset=0", "/agents?limit=200&offset=200", "/agents?limit=200&offset=400"]
    assert len(out["items"]) == 450 and out["items"][-1] == {"id": "a-449"} and out["total"] == 450
    assert out["pages"] == 3 and out["truncated"] is False and out["error"] is None


def test_the_path_keeps_its_own_query_after_the_page(ctx):
    _, urls, _ = _run(ctx, _rows(201), path="/sessions?agent_id=a%201&order_by=id:asc")
    assert urls == ["/sessions?limit=200&offset=0&agent_id=a%201&order_by=id:asc", "/sessions?limit=200&offset=200&agent_id=a%201&order_by=id:asc"]


def test_an_empty_list_is_one_request(ctx):
    out, urls, _ = _run(ctx, _rows(0))
    assert urls == ["/agents?limit=200&offset=0"]
    assert out["items"] == [] and out["total"] == 0 and out["truncated"] is False and out["pages"] == 1


def test_an_empty_page_after_full_ones_ends_the_list(ctx):
    """400 rows and no total: two full pages, then the empty third one says the list has ended."""
    out, urls, _ = _run(ctx, _rows(400))
    assert len(urls) == 3 and len(out["items"]) == 400 and out["truncated"] is False


def test_the_servers_total_ends_the_list_without_the_empty_page(ctx):
    out, urls, _ = _run(ctx, _rows(400, server_total="400"))
    assert len(urls) == 2 and len(out["items"]) == 400 and out["serverTotal"] == 400 and out["truncated"] is False


def test_a_repeated_row_is_listed_once(ctx):
    """A row created (sorting before the offset) between page 1 and page 2 shifts page 2 back by one: 250 rows come back as 251. The repeat is dropped."""
    out, urls, _ = _run(
        ctx,
        "var base = offset === 0 ? 0 : offset - 1; var items = [];"
        " for (var i = base; i < Math.min(250, base + limit); i++) items.push({ id: 'a-' + String(i).padStart(4, '0') });"
        " return Promise.resolve({ items: items });",
    )
    ids = [r["id"] for r in out["items"]]
    assert len(urls) == 2 and len(ids) == 250 and ids == sorted(set(ids)) and out["total"] == 250


def test_a_repeated_page_ends_the_list(ctx):
    """A server that ignores the offset answers every page with the first: a full page that adds no new id ends the listing after two requests, not 50."""
    out, urls, _ = _run(ctx, "var items = []; for (var i = 0; i < 200; i++) items.push({ id: 'a-' + i }); return Promise.resolve({ items: items });")
    assert len(urls) == 2 and len(out["items"]) == 200 and out["truncated"] is False


def test_a_server_that_answers_everything_at_once_is_asked_once(ctx):
    """GET /v1/triggers ignores limit and offset and answers every row with its total: the total ends the listing."""
    out, urls, _ = _run(
        ctx,
        "var items = []; for (var i = 0; i < 230; i++) items.push({ id: 't-' + i }); return Promise.resolve({ items: items, total: 230 });",
        path="/triggers",
    )
    assert urls == ["/triggers?limit=200&offset=0"] and len(out["items"]) == 230


def test_rows_are_told_apart_by_the_id_key_given(ctx):
    """A workspace's sessions are keyed by session_id: with the default key every row would look id-less, and a repeated page would not end the list."""
    out, urls, _ = _run(
        ctx,
        "var items = []; for (var i = 0; i < 200; i++) items.push({ session_id: 's-' + i }); return Promise.resolve({ items: items });",
        path="/workspaces/w/sessions", opts="{ idKey: 'session_id' }",
    )
    assert len(urls) == 2 and len(out["items"]) == 200


def test_the_cap_keeps_what_was_read_and_says_so(ctx):
    out, urls, _ = _run(ctx, _rows(100000, server_total="100000"), opts="{ maxPages: 3 }")
    assert len(urls) == 3 and len(out["items"]) == 600
    assert out["truncated"] is True and out["error"] is None and out["serverTotal"] == 100000 and out["limit"] == 600
    assert js(ctx, "PR_pagedNote(OUT, 'agents')") == "Showing the first 600 of 100000 agents."


def test_the_cap_is_50_pages_by_default(ctx):
    out, urls, _ = _run(ctx, _rows(100000))
    assert len(urls) == 50 and len(out["items"]) == 10000 and out["truncated"] is True and out["limit"] == 10000
    assert js(ctx, "PR_pagedNote(OUT, 'agents')") == "Showing the first 10000 agents; there may be more."


def test_a_full_last_page_at_the_total_is_not_truncated(ctx):
    out, urls, _ = _run(ctx, _rows(600, server_total="600"), opts="{ maxPages: 3 }")
    assert len(urls) == 3 and out["truncated"] is False and js(ctx, "PR_pagedNote(OUT, 'agents')") is None


def test_an_error_mid_way_keeps_what_was_read(ctx):
    out, urls, err = _run(
        ctx,
        "if (offset >= 400) return Promise.reject(new window.primerApi.ApiError({ status: 500, title: 'Internal Server Error', detail: 'the store is down' }));"
        + _rows(1000),
    )
    assert err is None and len(urls) == 3
    assert len(out["items"]) == 400 and out["truncated"] is True and out["pages"] == 2 and out["error"] == "the store is down"
    assert js(ctx, "PR_pagedNote(OUT, 'workspaces')") == "Showing the first 400 workspaces; the rest did not load: the store is down"


def test_an_error_on_the_first_page_is_the_lists_error(ctx):
    out, urls, err = _run(
        ctx, "return Promise.reject(new window.primerApi.ApiError({ status: 503, title: 'Service Unavailable', detail: 'down' }));")
    assert out is None and len(urls) == 1 and err == {"name": "ApiError", "message": "Service Unavailable", "status": 503}


def test_an_aborted_read_is_not_kept(ctx):
    """useResource aborts a superseded read: a page that fails because of that must reject, not hand back the rows read before it."""
    ctx.eval("var CTRL = { aborted: false };")
    out, urls, err = _run(
        ctx,
        "if (offset >= 200) { CTRL.aborted = true; var e = new Error('aborted'); e.name = 'AbortError'; return Promise.reject(e); }" + _rows(500),
        opts="{ signal: CTRL }",
    )
    assert out is None and err["name"] == "AbortError" and len(urls) == 2


def test_the_signal_reaches_every_request(ctx):
    ctx.eval("var SIGNALS = []; var SIG = { aborted: false };")
    ctx.eval(
        "PR_fetchAllPages('/agents', { signal: SIG, apiFetch: function (m, p, b, o) { SIGNALS.push(o.signal === SIG); var off = Number(/offset=(\\d+)/.exec(p)[1]);"
        " var items = []; for (var i = 0; i < (off ? 3 : 200); i++) items.push({ id: off + '-' + i }); return Promise.resolve({ items: items }); } });"
    )
    assert js(ctx, "SIGNALS") == [True, True]


def test_the_page_size_can_be_smaller(ctx):
    out, urls, _ = _run(ctx, _rows(25), opts="{ pageSize: 10 }")
    assert urls == ["/agents?limit=10&offset=0", "/agents?limit=10&offset=10", "/agents?limit=10&offset=20"] and len(out["items"]) == 25


def test_it_reads_through_the_consoles_apifetch_by_default(ctx):
    ctx.eval(
        "var SEEN = []; window.primerApi.apiFetch = function (m, p) { SEEN.push([m, p]); return Promise.resolve({ items: [{ id: 'x' }], total: 1 }); };"
        "var GOT = null; PR_fetchAllPages('/graphs').then(function (r) { GOT = r; });"
    )
    assert js(ctx, "SEEN") == [["GET", "/graphs?limit=200&offset=0"]] and js(ctx, "GOT.items") == [{"id": "x"}]


def test_no_note_for_a_whole_list_or_no_list(ctx):
    assert js(ctx, "PR_pagedNote({ items: [], truncated: false }, 'agents')") is None
    assert js(ctx, "PR_pagedNote(undefined, 'agents')") is None
    assert js(ctx, "PR_pagedNote({ items: [1, 2], truncated: true, serverTotal: null, error: null })") == "Showing the first 2 rows; there may be more."


# ---- round 2 of #923: never a silent truncation, whatever the server does -------------------------------------------------------------------------------


def test_a_server_that_clamps_the_limit_is_read_on_from_where_it_stopped(ctx):
    """A server answering 100 rows to limit=200 with total 450: a short page is not the end while the total says there is more; the offset
    advances by the rows answered."""
    out, urls, _ = _run(ctx, "var n = Math.max(0, Math.min(100, 450 - offset)); var items = [];"
                             " for (var i = 0; i < n; i++) items.push({ id: 'a-' + (offset + i) }); return Promise.resolve({ items: items, total: 450 });")
    assert [u.split("offset=")[1] for u in urls] == ["0", "100", "200", "300", "400"]
    assert len(out["items"]) == 450 and out["truncated"] is False and js(ctx, "PR_pagedNote(OUT, 'agents')") is None


def test_a_server_that_ignores_the_offset_and_counts_450_is_said(ctx):
    out, urls, _ = _run(ctx, "var items = []; for (var i = 0; i < limit; i++) items.push({ id: 'a-' + i }); return Promise.resolve({ items: items, total: 450 });")
    assert len(urls) == 2 and len(out["items"]) == 200 and out["truncated"] is True
    assert js(ctx, "PR_pagedNote(OUT, 'agents')") == "Showing the first 200 of 450 agents."


def test_a_page_that_repeats_the_one_before_under_churn_is_said(ctx):
    """Heavy churn: 200 rows created before the offset between two reads, so page 2 repeats page 1 exactly (total 600): the read stops, and says so."""
    out, urls, _ = _run(ctx, "var base = offset === 200 ? 0 : offset; var items = [];"
                             " for (var i = base; i < Math.min(600, base + limit); i++) items.push({ id: 'a-' + i });"
                             " return Promise.resolve({ items: items, total: 600 });")
    assert len(urls) == 2 and len(out["items"]) == 200 and out["truncated"] is True and out["serverTotal"] == 600
    assert js(ctx, "PR_pagedNote(OUT, 'agents')") == "Showing the first 200 of 600 agents."


def test_rows_without_an_id_from_a_server_that_ignores_the_offset_stop_after_the_repeat(ctx):
    """With no idKey on the rows every page used to look new, and the read ran to the cap (50 requests, 10000 copies of 200 rows)."""
    out, urls, _ = _run(ctx, "var items = []; for (var i = 0; i < 200; i++) items.push({ session_id: 's-' + i }); return Promise.resolve({ items: items });")
    assert len(urls) == 2 and len(out["items"]) == 200


def test_a_read_that_ends_short_of_the_total_is_truncated(ctx):
    """The server counted 450 but answered an empty page at 440 (rows deleted mid-read): fewer rows than the total is never whole."""
    out, _, _ = _run(ctx, "var n = Math.max(0, Math.min(limit, 440 - offset)); var items = [];"
                          " for (var i = 0; i < n; i++) items.push({ id: 'a-' + (offset + i) }); return Promise.resolve({ items: items, total: 450 });")
    assert len(out["items"]) == 440 and out["truncated"] is True
    assert js(ctx, "PR_pagedNote(OUT, 'agents')") == "Showing the first 440 of 450 agents."


def test_the_note_says_one_row_in_the_singular(ctx):
    assert js(ctx, "PR_pagedNote({ items: [1], truncated: true, serverTotal: null, error: 'x' }, 'channel providers')") == (
        "Showing the first 1 channel provider; the rest did not load: x")
    assert js(ctx, "PR_pagedNote({ items: [1], truncated: true, serverTotal: null, error: null }, 'approval policies')") == (
        "Showing the first 1 approval policy; there may be more.")
    assert js(ctx, "PR_pagedNote({ items: [1], truncated: true, serverTotal: null, error: null }, ['harness', 'harnesses'])") == (
        "Showing the first 1 harness; there may be more.")
    assert js(ctx, "PR_pagedNote({ items: [1], truncated: true, serverTotal: 9, error: null }, ['harness', 'harnesses'])") == (
        "Showing the first 1 of 9 harnesses.")
    assert js(ctx, "[PR_nounFor('harnesses', 1), PR_nounFor('workspaces', 1), PR_nounFor('workspaces', 2)]") == ["harness", "workspace", "workspaces"]


def test_a_cut_short_answer_carries_its_failure_for_a_refresh(ctx):
    """PR_partialFailure: the failure that cut a read short (useResource keeps a list it already shows); null for a whole read or the cap."""
    _run(ctx, "if (offset >= 200) return Promise.reject(new window.primerApi.ApiError({ status: 503, title: 'Service Unavailable', detail: 'blip' }));"
         + _rows(1000))
    assert js(ctx, "PR_partialFailure(OUT).status") == 503 and js(ctx, "PR_partialFailure(OUT).detail") == "blip"
    assert js(ctx, "PR_partialFailure(Object.assign({}, OUT)).message") == "blip", "a copied answer loses the failure, not its words"
    assert js(ctx, "PR_partialFailure({ items: [], truncated: true, error: null })") is None
    assert js(ctx, "PR_partialFailure({ items: [], truncated: false, error: null })") is None
    assert js(ctx, "PR_partialFailure(undefined)") is None


@pytest.mark.parametrize("base,pages,want", [(15000, 1, 15000), (15000, 3, 45000), (15000, 5, 60000), (3000, 50, 60000), (5000, 2, 10000),
                                             (120000, 3, 120000), (0, 3, 0), (None, 3, None)])
def test_a_lengthened_poll_never_passes_a_minute(ctx, base, pages, want):
    assert js(ctx, f"PR_pagedPollMs({json.dumps(base)}, {pages})") == want


# ---- the note and the poll, on the hook runtime -----------------------------------------------------------------------------------------------------

_HOOK_PRELUDE = r"""
var DATA = undefined; var OPTS = [];
window.primerApi = { useResource: function (key, fn, opts) { OPTS.push([key, opts.pollMs]); return { data: DATA, loading: false, error: null }; } };
function Probe(props) { var res = PR_usePagedResource("k", function () {}, { pollMs: props.pollMs, deps: [] }); return React.createElement("i", null, "x"); }
function Note(props) { return React.createElement("div", null, React.createElement(PR_PagedNote, { list: props.list, noun: "workspaces", testid: props.testid })); }
"""


@pytest.fixture
def hooks():
    c = mini_react_context(API, _HOOK_PRELUDE)
    try:
        yield c
    finally:
        c.close()


def test_a_polled_list_of_several_pages_polls_that_many_times_slower(hooks):
    """Ruling 4: a poll must not multiply the load. One request per tick before the fix; reading 3 pages per read, the cadence is 3 times longer, so the
    list still costs one page per base tick. A list of one page polls as it did."""
    hooks.eval("MR.mount(Probe, { pollMs: 15000 });")
    assert js(hooks, "OPTS[OPTS.length - 1]") == ["k", 15000]
    hooks.eval("DATA = { items: [], pages: 3 }; MR.rerender();")
    assert js(hooks, "OPTS[OPTS.length - 1]") == ["k", 45000]
    hooks.eval("DATA = { items: [], pages: 1 }; MR.rerender();")
    assert js(hooks, "OPTS[OPTS.length - 1]") == ["k", 15000]
    hooks.eval("DATA = { items: [], pages: 7 }; MR.rerender();")
    assert js(hooks, "OPTS[OPTS.length - 1]") == ["k", 60000], "never longer than a minute"


@pytest.mark.parametrize("poll", [0, None])
def test_a_list_that_does_not_poll_is_left_alone(hooks, poll):
    hooks.eval(f"MR.mount(Probe, {{ pollMs: {json.dumps(poll)} }}); DATA = {{ items: [], pages: 4 }}; MR.rerender();")
    assert js(hooks, "OPTS[OPTS.length - 1]") == ["k", poll]


def test_the_note_is_drawn_only_when_the_list_was_cut_short(hooks):
    hooks.eval("MR.mount(Note, { list: { items: [1, 2, 3], truncated: false } });")
    assert js(hooks, "MR.find('pr-paged-note')") is None
    hooks.eval("MR.rerender({ list: { items: [1, 2, 3], truncated: true, serverTotal: 9, error: null }, testid: 'ws-note' });")
    note = hooks.eval("JSON.stringify({ role: MR.find('ws-note').props.role, cls: MR.find('ws-note').props.className })")
    assert json.loads(note) == {"role": "note", "cls": "pr-paged-note"}
    assert js(hooks, "MR.texts()") == ["Showing the first 3 of 9 workspaces."]
