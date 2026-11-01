"""A failed knowledge tree read shows its refusal and a Retry (UI-L9a).

``KN_Tree`` (``ui/components/knowledge.jsx``) rendered an empty column when its
tree read failed: with no data, ``items`` fell back to ``[]`` and nothing told
the operator that the read died, let alone let them retry. It now renders the
refusal's words and a Retry on the resource's refetch.

MiniRacer on the real file: the ``useResource`` stub hands the tree key a
snapshot the test swaps (the failure, and a landed one for the characterisation),
and the context loads the real ``ui/foundation/api.js`` so ``readRefusal`` is the
house reader, not a stand-in.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE = ROOT / "ui" / "components" / "knowledge.jsx"
API_JS = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")

PRELUDE = r"""
globalThis.Btn = function (props) {
  return React.createElement("button", { "data-testid": props["data-testid"], onClick: props.onClick });
};
globalThis.Icon = function () { return null; };
globalThis.Banner = function (props) { return React.createElement("div", { "data-testid": "kn-banner" }, props.children); };
var TREE = null;      // the snapshot the useResource stub returns for the kn:tree: keys
var REFRESH = 0;
window.primerApi = {
  useResource: function (key) {
    if (key.indexOf("kn:tree:") === 0) {
      return TREE || { data: undefined, loading: true, error: null, degraded: false, refetch: function () {} };
    }
    return { data: undefined, loading: false, error: null, degraded: false, refetch: function () {} };
  },
  apiFetch: function () { return Promise.resolve({}); },
};
"""

DRIVER = r"""
function __msg() { return window.primerApi.readRefusal(window.__err).message; }
function __text() { return MR.texts().join(" "); }
"""

ERR = {"status": 403, "title": "Forbidden", "detail": "read-only collection"}


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _ctx(tree):
    c = mini_react_context(transpile(KNOWLEDGE), PRELUDE)
    c.eval(API_JS)  # merges into the prelude's primerApi: the real readRefusal
    c.eval(DRIVER)
    c.eval("window.__err = " + json.dumps(ERR) + ";")
    c.eval("TREE = " + json.dumps(tree) + ";")
    c.eval("TREE.refetch = function () { REFRESH++; };")
    c.eval(
        "MR.mount(KN_Tree, { cid: 'c1', parent: '.', depth: 0, expanded: {}, "
        "toggle: function () {}, selectedPath: null, onSelect: function () {}, reloadKey: 0 });"
    )
    return c


def test_a_failed_tree_read_shows_the_refusal_and_a_retry() -> None:
    # Red (UI-L9a): the read failed with no data; the column must say why and
    # offer the retry, not render empty.
    ctx = _ctx(
        {"data": None, "loading": False, "error": ERR, "degraded": False}
    )
    try:
        assert _js(ctx, "MR.find('kn-tree-retry')") is not None, "a failed tree read offers no retry"
        assert _js(ctx, "__msg()") in _js(ctx, "__text()"), "the refusal's words are not shown"
        ctx.eval("MR.click('kn-tree-retry');")
        assert _js(ctx, "REFRESH") == 1, "the Retry does not refetch the tree read"
    finally:
        ctx.close()


def test_a_landing_tree_read_shows_its_nodes() -> None:
    # Characterisation: the read lands and the column renders its nodes.
    ctx = _ctx(
        {"data": {"nodes": [{"path": "docs/a", "slug": "a", "title": "Doc A", "has_children": False}]},
         "loading": False, "error": None, "degraded": False}
    )
    try:
        assert "Doc A" in _js(ctx, "__text()")
        assert _js(ctx, "MR.find('kn-tree-retry')") is None
    finally:
        ctx.close()


def test_an_in_flight_tree_read_shows_loading() -> None:
    # Characterisation: while the first read is out, the column says loading.
    ctx = _ctx({"data": None, "loading": True, "error": None, "degraded": False})
    try:
        assert "Loading" in _js(ctx, "__text()")
        assert _js(ctx, "MR.find('kn-tree-retry')") is None
    finally:
        ctx.close()
