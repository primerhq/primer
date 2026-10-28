"""G10-5 + G10-6 (audit): the indexing error state and the missing
collection-level delete.

G10-5: when a collection's search backfill fails, the persisted state is
``error`` with the raw exception string and the partial index intact, and
the server's documented recovery is re-issuing the same
``PUT /collections/{id}/search`` (``primer/api/routers/knowledge.py``) -
yet the console's error branch offers no Retry and the banner carries no
recovery sentence (the only escape is Disable, which drops the vectors and
the config). G10-6: the server has ``DELETE /collections/{id}`` (403
system / 409 active writer / 502 unreachable store) but the console has
no collection-level delete anywhere - create/read/update are in the UI,
delete is API-only; system collections must not render it.

Source scans scoped to the real functions, plus MiniRacer over the
extracted delete handler with a capturing fetch stub (scalar returns).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "knowledge.jsx").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    """The source of the top-level function ``name`` (param parens balanced, then the body brace-matched)."""
    prefix = f"function {name}("
    i = SRC.index(prefix)
    k = i + len(prefix) - 1
    depth = 0
    while True:
        if SRC[k] == "(":
            depth += 1
        elif SRC[k] == ")":
            depth -= 1
            if depth == 0:
                break
        k += 1
    brace = SRC.index("{", k)
    depth = 0
    for k in range(brace, len(SRC)):
        if SRC[k] == "{":
            depth += 1
        elif SRC[k] == "}":
            depth -= 1
            if depth == 0:
                return SRC[i:k + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def _handler(src: str, name: str) -> str:
    """The source of ``const name = async () => { ... };`` (brace-matched)."""
    i = src.index(f"const {name} = async () => {{")
    depth = 0
    for k in range(i, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f"unbalanced braces in {name}")


# --- G10-5: the indexing error state ---------------------------------------------


def test_the_error_branch_offers_a_retry() -> None:
    assert "Retry" in _fn("KN_IndexingStrip"), "the error branch has no retry affordance"


def test_the_error_banner_carries_the_recovery_sentence() -> None:
    text = _fn("KN_IndexingStrip").lower()
    assert "re-issuing the same request retries" in text
    assert "partial index stays" in text


def test_the_retry_reeissues_the_put_with_the_current_config() -> None:
    strip = _fn("KN_IndexingStrip")
    puts = re.findall(r'apiFetch\(\s*"PUT"', strip)
    assert len(puts) >= 2, f"the enable PUT plus a retry PUT expected, found {len(puts)}"
    assert "collection.search.embedder" in strip, "the retry re-issues the collection's CURRENT embedder"
    assert "collection.search.vector_store_provider_id" in strip, "...and its current vector store"


# --- G10-6: the collection-level delete -------------------------------------------


def test_the_detail_offers_a_collection_level_delete() -> None:
    detail = _fn("KN_CollectionDetail")
    m = re.search(r'apiFetch\(\s*"DELETE",\s*`/collections/\$\{encodeURIComponent\(cid\)\}`', detail)
    assert m, "no collection-level DELETE in the detail overlay (only /search and /docs exist)"


def test_the_delete_verb_renders_only_for_non_system_collections() -> None:
    detail = _fn("KN_CollectionDetail")
    i = detail.index("readOnly ? null : (")
    frag = detail[i:i + 500]
    assert "Delete" in frag, "the footer's non-system branch has no Delete verb"


def test_confirming_the_delete_issues_the_delete_request() -> None:
    from py_mini_racer import MiniRacer

    handler = _handler(_fn("KN_CollectionDetail"), "removeCollection")
    ctx = MiniRacer()
    ctx.eval(
        """
var window = globalThis;
var CALLS = [], CONFIRM = null;
window.confirmDialog = function (cfg) { CONFIRM = cfg; return Promise.resolve(true); };
window.primerApi = { readRefusal: function (e) { return { message: (e && e.message) || "refused" }; } };
var apiFetch = function (m, p, body) { CALLS.push([m, p, JSON.stringify(body)]); return Promise.resolve({}); };
var pushToast = function () {};
var onBack = function () {};
var cid = "c-1";
"""
    )
    ctx.eval(handler)
    ctx.eval("removeCollection()")
    ctx.eval("void 0")
    calls = json.loads(ctx.eval("JSON.stringify(CALLS)"))
    confirm = json.loads(ctx.eval("JSON.stringify(CONFIRM)"))
    ctx.close()
    assert calls == [["DELETE", "/collections/c-1", "null"]], calls
    assert confirm and confirm["title"] == "Delete collection" and confirm["danger"] is True, confirm
