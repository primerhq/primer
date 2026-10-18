"""A toolset detail that answers 404 says "Toolset not found" (plan 06 Task 4).

The catalogue once listed a toolset no provider registers (``search``), and its
card opened "Not Found: Toolset 'search' does not exist"; an old link to it, or a
toolset deleted while its card was on screen, lands on the same error branch. The
console now renders "Toolset not found" with a reason, and the retired ids
``search`` and ``_search`` say where search went. The unit test mounts the real
``ToolsetDetail`` with stubbed hooks (the 404 journey is
``tests/ui_e2e/test_toolset_not_found_journey.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

TOOLSETS = Path(__file__).resolve().parents[2] / "ui" / "components" / "toolsets.jsx"

_PRELUDE = """
var __navs = []; var __detail = null;
globalThis.Btn = function (props) { return React.createElement("button", props, props.children); };
globalThis.Icon = function () { return null; };
globalThis.Modal = function (props) { return React.createElement("div", null, props.children, props.footer); };
globalThis.FormField = function (props) { return React.createElement("div", null, props.children); };
globalThis.Banner = function (props) { return React.createElement("div", { "data-testid": "banner" }, props.title, props.detail, props.actions); };
window.primerApi = {
  useResource: function (key) {
    return key.indexOf("toolset-detail:") === 0 ? __detail : { loading: false, data: null, error: null, refetch: function () {} };
  },
  useMutation: function () { return { loading: false, mutate: function () { return Promise.resolve(); } }; },
  useRouter: function () { return { params: {}, query: {}, navigate: function (p) { __navs.push(p); } }; },
  apiFetch: function () { return Promise.resolve({}); },
};
"""

_TEXT = """
function __text(n) {
  if (n == null || typeof n === "boolean") return "";
  if (typeof n === "string" || typeof n === "number") return String(n);
  if (Array.isArray(n)) return n.map(__text).join("");
  if (n.__el) return __text(typeof n.type === "function" && n.type !== React.Fragment ? n.out : n.children);
  return "";
}
"""

_OPEN: list = []


@pytest.fixture(autouse=True)
def _close_contexts():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _mount(tid: str, error: dict):
    ctx = mini_react_context(transpile(TOOLSETS), _PRELUDE)
    _OPEN.append(ctx)
    ctx.eval(_TEXT)
    ctx.eval("__detail = { loading: false, data: null, error: " + json.dumps(error) + ", refetch: function () {} };")
    ctx.eval("MR.mount(ToolsetDetail, { toolsetId: " + json.dumps(tid) + ", pushToast: function () {} });")
    return ctx


NOT_FOUND = {"status": 404, "title": "Not Found", "detail": "Toolset 'gone-one' does not exist"}
SEARCH_REASON = ("There is no search toolset. Agents search this install's agents, graphs, collections and tools with the "
                 "Collections toolset's search tool, by meaning once internal search is on in System \u203a Internal collections.")


def test_a_missing_toolset_says_not_found_with_a_reason() -> None:
    ctx = _mount("gone-one", NOT_FOUND)
    text = ctx.eval("__text(MR.find('toolset-not-found'))")
    assert "Toolset not found" in text
    assert ctx.eval("__text(MR.find('toolset-not-found-reason'))") == (
        "There is no toolset named gone-one. It may have been deleted, or the link is out of date.")
    assert "Not Found" not in text and "does not exist" not in text


@pytest.mark.parametrize("tid", ["search", "_search"])
def test_the_retired_search_ids_explain_where_search_went(tid: str) -> None:
    ctx = _mount(tid, {"status": 404, "title": "Not Found", "detail": f"Toolset {tid!r} does not exist"})
    assert ctx.eval("__text(MR.find('toolset-not-found-reason'))") == SEARCH_REASON


def test_back_from_not_found_navigates_like_the_other_back_buttons() -> None:
    ctx = _mount("gone-one", NOT_FOUND)
    ctx.eval("MR.click('toolset-not-found-back')")
    assert json.loads(ctx.eval("JSON.stringify(__navs)")) == ["/toolsets"]


def test_a_server_error_keeps_the_error_banner() -> None:
    ctx = _mount("my-mcp", {"status": 500, "title": "Internal Server Error", "detail": "boom"})
    assert ctx.eval("MR.find('toolset-not-found') === null") is True
    assert "Internal Server Error" in ctx.eval("__text(MR.find('banner'))")


def test_the_not_found_copy_has_no_em_dash() -> None:
    ctx = _mount("search", NOT_FOUND)
    assert "\u2014" not in ctx.eval("__text(MR.find('toolset-not-found'))")
