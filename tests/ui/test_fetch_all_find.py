"""Every page of a FILTERED list (ticket 01a16eaa): ``PR_fetchAllFind`` in ``ui/foundation/api.js``, run in MiniRacer.

``PR_fetchAllFind`` is the find sibling of ``PR_fetchAllPages``: the same read-every-page loop (the same stop, cap, dedupe and
cut-short rules, pinned for the GET form by ``tests/ui/test_fetch_all_pages.py``) over the resource's ``POST /<plural>/find``
route. That route takes the ``FindRequest`` body (``primer/api/pagination.py``): the predicate (the server's filter query, or
``null`` for every row), an offset page and optional sort keys, and answers the same ``{kind: "offset", offset, length, total,
items}`` page shape the list routes answer. A panel that wants "the rows of this harness" or "the sessions bound to these
agents" pays one request per page of the ANSWER, not of the table: the toolset Sessions tab and the harnesses detail page
read the whole tables and filtered them client-side before (01a16eaa), and are the two consumers this file's component
tests pin.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
API = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")

PRED = '{"kind": "predicate", "left": {"kind": "field", "name": "harness_id"}, "op": "=", "right": {"kind": "value", "value": "h1"}}'


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


def _run(ctx, answer: str, path: str = "/agents/find", opts: str = "{}", predicate: str = PRED):
    """PR_fetchAllFind(path, pred) against a fake whose ``answer`` (JS with ``offset`` and ``limit`` in scope) is each page's
    response or a thrown error. CALLS records one entry per request: {method, path, body, signal}."""
    ctx.eval(
        "var CALLS = []; var OUT = null; var ERR = null;"
        "function fake(method, path, body, o) { CALLS.push({ method: method, path: path, body: body, signal: !!(o && o.signal) });"
        "  var offset = Number(body.page.offset); var limit = Number(body.page.length);"
        "  " + answer + " }"
        f"PR_fetchAllFind({json.dumps(path)}, {predicate}, Object.assign({{ apiFetch: fake }}, {opts}))"
        ".then(function (r) { OUT = r; }, function (e) { ERR = { name: e.name, message: e.message, status: e.status == null ? null : e.status }; });"
    )
    return js(ctx, "OUT"), js(ctx, "CALLS"), js(ctx, "ERR")


def _bodies(calls):
    return [c["body"] for c in calls]


def _rows(total: int, server_total: str = "null") -> str:
    """A server holding ``total`` rows a-0 .. a-<total-1>, in id order."""
    return (
        f"var n = Math.max(0, Math.min(limit, {total} - offset)); var items = [];"
        " for (var i = 0; i < n; i++) items.push({ id: 'a-' + (offset + i) });"
        f" return Promise.resolve({{ kind: 'offset', offset: offset, length: n, total: {server_total}, items: items }});"
    )


def test_it_reads_every_page_past_200(ctx):
    out, calls, err = _run(ctx, _rows(450))
    assert err is None
    assert calls[0]["method"] == "POST" and [c["path"] for c in calls] == ["/agents/find"] * 3
    assert [c["body"]["page"] for c in calls] == [
        {"kind": "offset", "offset": 0, "length": 200},
        {"kind": "offset", "offset": 200, "length": 200},
        {"kind": "offset", "offset": 400, "length": 200},
    ]
    assert len(out["items"]) == 450 and out["items"][-1] == {"id": "a-449"} and out["total"] == 450
    assert out["pages"] == 3 and out["truncated"] is False and out["error"] is None


def test_the_predicate_reaches_every_page(ctx):
    out, calls, err = _run(ctx, _rows(450), predicate=PRED)
    assert err is None and len(calls) == 3
    assert all(c["body"]["predicate"] == json.loads(PRED) for c in calls)


def test_the_sort_keys_reach_every_page_when_given(ctx):
    out, calls, err = _run(
        ctx,
        _rows(450),
        opts="{ order_by: [{ field: 'created_at', direction: 'desc' }] }",
    )
    assert err is None and len(calls) == 3
    assert all(c["body"]["order_by"] == [{"field": "created_at", "direction": "desc"}] for c in calls)


def test_a_null_predicate_lists_every_row(ctx):
    """FindRequest takes predicate null for "every row": the body says so, the read goes on as usual."""
    out, calls, err = _run(ctx, _rows(250), predicate="null")
    assert err is None and len(calls) == 2
    assert all(c["body"]["predicate"] is None for c in calls) and len(out["items"]) == 250


def test_an_empty_list_is_one_request(ctx):
    out, calls, err = _run(ctx, _rows(0))
    assert err is None and len(calls) == 1 and out["items"] == [] and out["truncated"] is False


def test_an_empty_page_after_full_ones_ends_the_list(ctx):
    """With no total from the server, an empty page is the end (the GET form's rule, over find)."""
    out, calls, err = _run(ctx, "var n = Math.max(0, Math.min(limit, 300 - offset)); var items = [];"
                                " for (var i = 0; i < n; i++) items.push({ id: 'a-' + (offset + i) });"
                                " return Promise.resolve({ kind: 'offset', offset: offset, length: n, items: items });")
    assert err is None and len(calls) == 2 and len(out["items"]) == 300 and out["truncated"] is False


def test_the_servers_total_ends_the_list_without_the_empty_page(ctx):
    out, calls, _ = _run(ctx, _rows(400, server_total="400"))
    assert len(calls) == 2 and len(out["items"]) == 400 and out["serverTotal"] == 400 and out["truncated"] is False


def test_a_repeated_row_is_listed_once(ctx):
    """A row created (sorting before the offset) between page 1 and page 2 shifts page 2 back by one: 250 rows come back as 251. The repeat is dropped."""
    out, calls, _ = _run(
        ctx,
        "var base = offset === 0 ? 0 : offset - 1; var items = [];"
        " for (var i = base; i < Math.min(250, base + limit); i++) items.push({ id: 'a-' + String(i).padStart(4, '0') });"
        " return Promise.resolve({ items: items });",
    )
    ids = [r["id"] for r in out["items"]]
    assert len(calls) == 2 and len(ids) == 250 and ids == sorted(set(ids)) and out["total"] == 250


def test_a_repeated_page_ends_the_list(ctx):
    """A server that ignores the offset answers every page with the first: a full page that adds no new id ends the listing after two requests, not 50."""
    out, calls, _ = _run(ctx, "var items = []; for (var i = 0; i < 200; i++) items.push({ id: 'a-' + i }); return Promise.resolve({ items: items });")
    assert len(calls) == 2 and len(out["items"]) == 200 and out["truncated"] is False


def test_a_server_that_answers_everything_at_once_is_asked_once(ctx):
    """The server's total ends the listing, so a find that answers every row on the first page is asked once."""
    out, calls, _ = _run(
        ctx,
        "var items = []; for (var i = 0; i < 230; i++) items.push({ id: 't-' + i }); return Promise.resolve({ items: items, total: 230 });",
        path="/triggers/find",
    )
    assert len(calls) == 1 and len(out["items"]) == 230


def test_rows_are_told_apart_by_the_id_key_given(ctx):
    """A session's find is keyed by session_id: with the default key every row would look id-less, and a repeated page would not end the list."""
    out, calls, _ = _run(
        ctx,
        "var items = []; for (var i = 0; i < 200; i++) items.push({ session_id: 's-' + i }); return Promise.resolve({ items: items });",
        path="/sessions/find",
        opts="{ idKey: 'session_id' }",
    )
    assert len(calls) == 2 and len(out["items"]) == 200


def test_the_cap_keeps_what_was_read_and_says_so(ctx):
    out, calls, _ = _run(ctx, _rows(100000, server_total="100000"), opts="{ maxPages: 3 }")
    assert len(calls) == 3 and len(out["items"]) == 600
    assert out["truncated"] is True and out["error"] is None and out["serverTotal"] == 100000 and out["limit"] == 600
    assert js(ctx, "PR_pagedNote(OUT, 'agents')") == "Showing the first 600 of 100000 agents."


def test_the_cap_is_50_pages_by_default(ctx):
    out, calls, _ = _run(ctx, _rows(100000))
    assert len(calls) == 50 and len(out["items"]) == 10000 and out["truncated"] is True and out["limit"] == 10000
    assert js(ctx, "PR_pagedNote(OUT, 'agents')") == "Showing the first 10000 agents; there may be more."


def test_a_full_last_page_at_the_total_is_not_truncated(ctx):
    out, calls, _ = _run(ctx, _rows(600, server_total="600"), opts="{ maxPages: 3 }")
    assert len(calls) == 3 and out["truncated"] is False and js(ctx, "PR_pagedNote(OUT, 'agents')") is None


def test_an_error_mid_way_keeps_what_was_read(ctx):
    out, calls, err = _run(
        ctx,
        "if (offset >= 400) return Promise.reject(new window.primerApi.ApiError({ status: 500, title: 'Internal Server Error', detail: 'the store is down' }));"
        + _rows(1000),
    )
    assert err is None and len(calls) == 3
    assert len(out["items"]) == 400 and out["truncated"] is True and out["pages"] == 2 and out["error"] == "the store is down"
    assert js(ctx, "PR_pagedNote(OUT, 'workspaces')") == "Showing the first 400 workspaces; the rest did not load: the store is down"


def test_an_error_on_the_first_page_is_the_lists_error(ctx):
    out, calls, err = _run(
        ctx, "return Promise.reject(new window.primerApi.ApiError({ status: 503, title: 'Service Unavailable', detail: 'down' }));")
    assert out is None and len(calls) == 1 and err == {"name": "ApiError", "message": "Service Unavailable", "status": 503}


def test_an_aborted_read_is_not_kept(ctx):
    """useResource aborts a superseded read: a page that fails because of that must reject, not hand back the rows read before it."""
    ctx.eval("var CTRL = { aborted: false };")
    out, calls, err = _run(
        ctx,
        "if (offset >= 200) { CTRL.aborted = true; var e = new Error('aborted'); e.name = 'AbortError'; return Promise.reject(e); }" + _rows(500),
        opts="{ signal: CTRL }",
    )
    assert out is None and err["name"] == "AbortError" and len(calls) == 2


def test_the_signal_reaches_every_request(ctx):
    ctx.eval("var SIGNALS = []; var SIG = { aborted: false };")
    ctx.eval(
        "PR_fetchAllFind('/agents/find', " + PRED + ", { signal: SIG, apiFetch: function (m, p, b, o) { SIGNALS.push(o.signal === SIG); var off = Number(b.page.offset);"
        " var items = []; for (var i = 0; i < (off ? 3 : 200); i++) items.push({ id: off + '-' + i }); return Promise.resolve({ items: items }); } });"
    )
    assert js(ctx, "SIGNALS") == [True, True]


def test_the_page_size_can_be_smaller(ctx):
    out, calls, _ = _run(ctx, _rows(25), opts="{ pageSize: 10 }")
    assert [c["body"]["page"] for c in calls] == [
        {"kind": "offset", "offset": 0, "length": 10},
        {"kind": "offset", "offset": 10, "length": 10},
        {"kind": "offset", "offset": 20, "length": 10},
    ] and len(out["items"]) == 25


def test_it_reads_through_the_consoles_apifetch_by_default(ctx):
    ctx.eval(
        "var SEEN = []; window.primerApi.apiFetch = function (m, p, b) { SEEN.push([m, p]); return Promise.resolve({ items: [{ id: 'x' }], total: 1 }); };"
        "var GOT = null; PR_fetchAllFind('/graphs/find', " + PRED + ").then(function (r) { GOT = r; });"
    )
    assert js(ctx, "SEEN") == [["POST", "/graphs/find"]] and js(ctx, "GOT.items") == [{"id": "x"}]


def test_a_server_that_clamps_the_limit_is_read_on_from_where_it_stopped(ctx):
    """A server answering 100 rows to a page of 200 with total 450: a short page is not the end while the total says there is more; the offset
    advances by the rows answered."""
    out, calls, _ = _run(ctx, "var n = Math.max(0, Math.min(100, 450 - offset)); var items = [];"
                              " for (var i = 0; i < n; i++) items.push({ id: 'a-' + (offset + i) }); return Promise.resolve({ items: items, total: 450 });")
    assert [c["body"]["page"]["offset"] for c in calls] == [0, 100, 200, 300, 400]
    assert len(out["items"]) == 450 and out["truncated"] is False and js(ctx, "PR_pagedNote(OUT, 'agents')") is None


def test_a_server_that_ignores_the_offset_and_counts_450_is_said(ctx):
    out, calls, _ = _run(ctx, "var items = []; for (var i = 0; i < limit; i++) items.push({ id: 'a-' + i }); return Promise.resolve({ items: items, total: 450 });")
    assert len(calls) == 2 and len(out["items"]) == 200 and out["truncated"] is True
    assert js(ctx, "PR_pagedNote(OUT, 'agents')") == "Showing the first 200 of 450 agents."


def test_a_read_that_ends_short_of_the_total_is_truncated(ctx):
    """The server counted 450 but answered an empty page at 440 (rows deleted mid-read): fewer rows than the total is never whole."""
    out, _, _ = _run(ctx, "var n = Math.max(0, Math.min(limit, 440 - offset)); var items = [];"
                          " for (var i = 0; i < n; i++) items.push({ id: 'a-' + (offset + i) }); return Promise.resolve({ items: items, total: 450 });")
    assert len(out["items"]) == 440 and out["truncated"] is True
    assert js(ctx, "PR_pagedNote(OUT, 'agents')") == "Showing the first 440 of 450 agents."


def test_a_cut_short_answer_carries_its_failure_for_a_refresh(ctx):
    """PR_partialFailure: the failure that cut a read short (useResource keeps a list it already shows); null for a whole read or the cap."""
    _run(ctx, "if (offset >= 200) return Promise.reject(new window.primerApi.ApiError({ status: 503, title: 'Service Unavailable', detail: 'blip' }));"
         + _rows(1000))
    assert js(ctx, "PR_partialFailure(OUT).status") == 503 and js(ctx, "PR_partialFailure(OUT).detail") == "blip"
    assert js(ctx, "PR_partialFailure(Object.assign({}, OUT)).message") == "blip", "a copied answer loses the failure, not its words"
