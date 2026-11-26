"""Review 3.8, UI half: the wiki palette's document listing follows the server's cursor.

``GET /v1/collections/{id}/documents`` answers at most 1000 entries per page with
``next_after_path`` (lane M1 part 2). ``SH_api.collectionDocuments`` (the seam the palette's
wiki source reads) used to return only the first page, so a collection past 1000 documents
silently lost rows in the palette. It now follows ``next_after_path`` up to
``SH_COLLECTION_DOCS_MAX`` entries and answers ``more: true`` when it stopped short.

Runs the real seam in V8 against a scripted fetch; the pending pages are pumped by hand the
way ``test_row_reads`` pumps its reads.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from tests.ui._mini_react import ROOT

SH_API = (ROOT / "ui" / "components" / "shell" / "sh-api.jsx").read_text(encoding="utf-8")

PRELUDE = r"""
var window = globalThis;
var REQUESTS = [], PAGES = [], PI = 0, PEND = [], RESULT = null;
window.primerApi = {
  apiFetch: function (method, url) {
    REQUESTS.push(url);
    return new Promise(function (resolve) { PEND.push(resolve); });
  },
};
function pump() {
  if (!PEND.length) return false;
  PEND.shift()(PAGES[PI++]);
  return true;
}
function start(cid) {
  return SH_api.collectionDocuments(cid).then(function (d) { RESULT = d; });
}
"""


@pytest.fixture
def ctx() -> Iterator[object]:
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    c.eval(PRELUDE)
    c.eval(SH_API)
    try:
        yield c
    finally:
        c.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify((function () { return " + expr + "; })())"))


def _page(paths, next_after):
    return {
        "documents": [{"path": p, "document_id": "d-" + p, "size": 1} for p in paths],
        "next_after_path": next_after,
    }


def test_the_seam_follows_the_cursor_to_the_last_page(ctx) -> None:
    ctx.eval("PAGES = " + json.dumps([
        _page(["a", "b"], "b"),
        _page(["c", "d"], "d"),
        _page(["e"], None),
    ]) + ";")
    ctx.eval("start('system');")
    for _ in range(3):
        assert js(ctx, "pump()") is True
    assert js(ctx, "RESULT.more") is False
    assert [d["path"] for d in js(ctx, "RESULT.documents")] == ["a", "b", "c", "d", "e"]
    assert js(ctx, "REQUESTS.length") == 3
    assert js(ctx, "REQUESTS[0]") == "/collections/system/documents"
    assert js(ctx, "REQUESTS[1]") == "/collections/system/documents?after_path=b"
    assert js(ctx, "REQUESTS[2]") == "/collections/system/documents?after_path=d"


def test_a_single_page_needs_no_more_requests(ctx) -> None:
    ctx.eval("PAGES = " + json.dumps([_page(["a", "b"], None)]) + ";")
    ctx.eval("start('system');")
    assert js(ctx, "pump()") is True
    assert js(ctx, "pump()") is False, "no second request once the cursor is null"
    assert js(ctx, "RESULT.more") is False
    assert js(ctx, "REQUESTS.length") == 1


def test_stopping_at_the_cap_reports_more(ctx) -> None:
    ctx.eval("SH_COLLECTION_DOCS_MAX = 4;")
    ctx.eval("PAGES = " + json.dumps([
        _page(["a", "b"], "b"),
        _page(["c", "d"], "d"),
        _page(["e"], None),
    ]) + ";")
    ctx.eval("start('system');")
    assert js(ctx, "pump()") is True
    assert js(ctx, "pump()") is True
    assert js(ctx, "pump()") is False, "the cap stops the follow before the third page"
    assert js(ctx, "RESULT.more") is True, "the answer must say rows exist past the cap"
    assert [d["path"] for d in js(ctx, "RESULT.documents")] == ["a", "b", "c", "d"]
    assert js(ctx, "REQUESTS.length") == 2


def test_a_repeated_cursor_stops_the_follow(ctx) -> None:
    # A server that answers the same next_after_path twice would otherwise be followed
    # forever: the seam stops at the repeat and still says there is more.
    ctx.eval("PAGES = " + json.dumps([
        _page(["a"], "a"),
        _page(["b"], "a"),
    ]) + ";")
    ctx.eval("start('system');")
    assert js(ctx, "pump()") is True
    assert js(ctx, "pump()") is True
    assert js(ctx, "pump()") is False, "a non-advancing cursor must stop the follow"
    assert js(ctx, "RESULT.more") is True
    assert [d["path"] for d in js(ctx, "RESULT.documents")] == ["a", "b"]
    assert js(ctx, "REQUESTS.length") == 2
