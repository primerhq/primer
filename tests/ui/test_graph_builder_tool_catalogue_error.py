"""A tool step can be given a tool when the tool catalogue cannot be loaded (board ticket 01a11e4e-7115, found in the lead's review of #633).

The retired legacy editor fell back to a raw ``tool_id`` text input when ``GET /tools/catalogue`` failed. The builder ignored the request's error (``toolsRes.error``) and drew "No tools match.":
the same words as an empty search, so with the catalogue down a tool step could not be given a tool and nothing said why. Now, in every place a tool is picked (the inspector of a tool step,
the add-step palette, the starters) ``GB_ToolPicker`` says the list could not be loaded, with the server's words, offers a retry, and offers a text box for the tool's id; while the list is
loading it says so; and "No tools match." is only the answer to a search that matches nothing. The validator treats an id it cannot check as fine (it warns about an unlisted id only when it
has a catalogue to check against), so a typed id does not block Save.

The REAL builder runs in V8 on the strict mini React (``tests/ui/_graph_builder_v8.py`` has the harness); the catalogue request is stubbed to fail.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from tests.ui._graph_builder_v8 import DRIVER, PRELUDE, builder_code
from tests.ui._mini_react import mini_react_context
from tests.ui.test_graph_builder_import_shapes import BASE

ROOT = Path(__file__).resolve().parents[2]
PALETTE = (ROOT / "ui" / "components" / "graph-builder" / "gb-palette.jsx").read_text(encoding="utf-8")
STARTERS = (ROOT / "ui" / "components" / "graph-builder" / "gb-starters.jsx").read_text(encoding="utf-8")
INSPECTOR = (ROOT / "ui" / "components" / "graph-builder" / "gb-inspector.jsx").read_text(encoding="utf-8")
BUILDER = (ROOT / "ui" / "components" / "graph-builder" / "graph-builder.jsx").read_text(encoding="utf-8")

# the catalogue request fails (the other requests of the builder are answered as the shared prelude answers them)
FAILING = """
var __retries = 0;
window.primerApi.useResource = function (key) {
  if (key === "tools:catalogue") return { data: null, error: { status: 503, detail: "the tool service is down" }, loading: false, refetch: function () { __retries += 1; } };
  return { data: { items: TOOLS } };
};
"""
LOADING = FAILING.replace('error: { status: 503, detail: "the tool service is down" }, loading: false', "error: null, loading: true")
EMPTY = FAILING.replace('data: null, error: { status: 503, detail: "the tool service is down" }, loading: false', "data: { items: [] }, error: null, loading: false")


def _ctx(prelude: str = ""):
    c = mini_react_context(builder_code(), PRELUDE + prelude)
    c.eval(DRIVER)
    c.eval("function draftOf() { return inspector().props.draft; }")
    c.eval("function saveDisabled() { return MR.find('gb-save').props.disabled; }")
    return c


@pytest.fixture
def failing():
    c = _ctx(FAILING)
    try:
        yield c
    finally:
        c.close()


def _open(ctx, node: str = "t") -> None:
    ctx.eval(f"mountBuilder({json.dumps(copy.deepcopy(BASE))}); selectNode({json.dumps(node)});")


def _texts(ctx) -> list[str]:
    return json.loads(ctx.eval("JSON.stringify(MR.texts())"))


def _node(ctx, node_id: str = "t") -> dict:
    return json.loads(ctx.eval(f"JSON.stringify(draftOf().nodes.filter(function (n) {{ return n.id === {json.dumps(node_id)}; }})[0])"))


def test_a_catalogue_that_cannot_be_loaded_is_not_reported_as_an_empty_search(failing) -> None:
    _open(failing)
    texts = " ".join(_texts(failing))
    assert "No tools match" not in texts
    assert failing.eval("MR.find('gb-tool-catalogue-error') !== null") is True
    assert "the tool service is down" in texts, "the server's own words are shown"
    assert failing.eval("MR.find('gb-tool-catalogue-error').props.role") == "alert"


def test_the_tool_id_can_be_typed_and_it_sets_the_steps_tool(failing) -> None:
    _open(failing)
    shown = failing.eval("MR.find('gb-tool-id-input').props.value")
    assert shown == "ts__echo", "the box holds the id the step already has"
    failing.eval("MR.find('gb-tool-id-input').props.onChange({ target: { value: ' ts__other ' } }); MR.rerender();")
    assert _node(failing)["tool_id"] == "ts__other", "blanks are not part of an id"
    assert failing.eval("saveDisabled()") is False, "a typed id the builder cannot check does not block Save"


def test_the_id_box_is_named_and_the_error_is_described_to_it(failing) -> None:
    _open(failing)
    box = json.loads(failing.eval("JSON.stringify({ label: MR.find('gb-tool-id-input').props['aria-label'], describedby: MR.find('gb-tool-id-input').props['aria-describedby'], errid: MR.find('gb-tool-catalogue-error').props.id })"))
    assert box["label"] == "Tool id"
    assert box["errid"] and box["errid"] in (box["describedby"] or "").split()


def test_the_list_can_be_asked_for_again(failing) -> None:
    _open(failing)
    failing.eval("MR.click('gb-tool-catalogue-retry');")
    assert failing.eval("__retries") == 1


def test_a_tool_step_whose_fields_are_not_known_says_so_and_does_not_ask_to_choose_a_tool(failing) -> None:
    _open(failing)
    texts = _texts(failing)
    assert "Choose a tool to see what it needs." not in texts
    assert any("not listed" in s or "no fields" in s.lower() for s in texts), texts


def test_while_the_catalogue_is_loading_the_picker_says_so() -> None:
    ctx = _ctx(LOADING)
    try:
        _open(ctx)
        texts = " ".join(_texts(ctx))
        assert "Loading tools" in texts and "No tools match" not in texts
        assert ctx.eval("MR.find('gb-tool-id-input') === null") is True, "no text box until the load has failed"
    finally:
        ctx.close()


def test_a_loaded_catalogue_is_the_list_it_always_was() -> None:
    ctx = _ctx()
    try:
        _open(ctx)
        assert ctx.eval("MR.find('gb-tool-catalogue-error') === null") is True and ctx.eval("MR.find('gb-tool-id-input') === null") is True
        assert "ts__echo" in " ".join(_texts(ctx))
    finally:
        ctx.close()


def test_no_tools_match_is_only_the_answer_to_a_search_and_an_empty_catalogue_says_it_is_empty() -> None:
    ctx = _ctx(EMPTY)
    try:
        _open(ctx)
        texts = " ".join(_texts(ctx))
        assert "No tools match" not in texts and "No tools are available" in texts
    finally:
        ctx.close()
    ctx = _ctx()
    try:
        _open(ctx)
        search = ctx.eval("(function () { var hit = null; (function w(n) { if (hit || n == null || typeof n !== 'object') return; if (Array.isArray(n)) { n.forEach(w); return; } if (!n.__el) return;"
                          " if (n.props && n.props['aria-label'] === 'Search tools') hit = n; else if (typeof n.type === 'function' && n.type !== React.Fragment) w(n.out); else w(n.children); })(MR.find('gb-builder')); return hit !== null; })()")
        assert search is True
        ctx.eval("(function () { var hit = null; (function w(n) { if (hit || n == null || typeof n !== 'object') return; if (Array.isArray(n)) { n.forEach(w); return; } if (!n.__el) return;"
                 " if (n.props && n.props['aria-label'] === 'Search tools') hit = n; else if (typeof n.type === 'function' && n.type !== React.Fragment) w(n.out); else w(n.children); })(MR.find('gb-builder'));"
                 " hit.props.onChange({ target: { value: 'zzz-nothing' } }); MR.rerender(); })()")
        assert "No tools match" in " ".join(_texts(ctx))
    finally:
        ctx.close()


def test_the_picker_alone_shows_the_same_for_every_place_it_is_used() -> None:
    """The palette and the starters use the same ``GB_ToolPicker``; mounted directly it answers a failed catalogue the same way."""
    ctx = _ctx()
    try:
        ctx.eval("var __picked = []; var __retried = 0;")
        ctx.eval("MR.mount(GB_ToolPicker, { tools: [], value: '', onChange: function (v) { __picked.push(v); }, catalogue: { error: { detail: 'boom' }, loading: false, retry: function () { __retried += 1; } } });")
        assert "boom" in " ".join(_texts(ctx)) and ctx.eval("MR.find('gb-tool-id-input') !== null") is True
        ctx.eval("MR.find('gb-tool-id-input').props.onChange({ target: { value: 'a__b' } });")
        assert json.loads(ctx.eval("JSON.stringify(__picked)")) == ["a__b"]
        ctx.eval("MR.click('gb-tool-catalogue-retry');")
        assert ctx.eval("__retried") == 1
        # a stale list wins over a later error (the list is still good)
        ctx.eval("MR.mount(GB_ToolPicker, { tools: [{ id: 'x__y', description: 'd' }], value: '', onChange: function () {}, catalogue: { error: { detail: 'boom' }, loading: false } });")
        assert ctx.eval("MR.find('gb-tool-id-input') === null") is True and "x__y" in " ".join(_texts(ctx))
    finally:
        ctx.close()


@pytest.mark.parametrize(("name", "src", "pattern"), [
    ("the add-step palette", PALETTE, r"<GB_ToolPicker[^>]*catalogue=\{catalogue\}"),
    ("the starters", STARTERS, r"<window\.GB_ToolPicker[^>]*catalogue=\{catalogue\}"),
    ("the tool step's inspector", INSPECTOR, r"<GB_ToolPicker[^>]*catalogue=\{catalogue\}"),
], ids=["palette", "starters", "inspector"])
def test_every_place_a_tool_is_picked_is_handed_the_catalogues_state(name: str, src: str, pattern: str) -> None:
    assert re.search(pattern, src), f"{name} does not pass the catalogue state to GB_ToolPicker"


def test_the_builder_hands_its_catalogue_request_to_the_inspector_the_palette_and_the_starters() -> None:
    assert "catalogue={" in BUILDER and BUILDER.count("catalogue={catalogue}") >= 3
    assert "refetch" in BUILDER[BUILDER.index("const catalogue"):BUILDER.index("const catalogue") + 400]
