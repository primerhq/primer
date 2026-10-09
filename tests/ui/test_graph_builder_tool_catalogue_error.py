"""A tool step can be given a tool when the tool catalogue cannot be loaded (board ticket 01a11e4e-7115, found in the lead's review of #633).

The retired legacy editor fell back to a raw ``tool_id`` text input when ``GET /tools/catalogue`` failed. The builder ignored the request's error (``toolsRes.error``) and drew "No tools match.":
the same words as an empty search, so with the catalogue down a tool step could not be given a tool and nothing said why. Now, in every place a tool is picked (the inspector of a tool step,
the add-step palette, the starters) ``GB_ToolPicker`` says the list could not be loaded, with the server's words, offers a retry, and offers a text box for the tool's id; while the list is
loading it says so; and "No tools match." is only the answer to a search that matches nothing. The validator treats an id it cannot check as fine (it warns about an unlisted id only when it
has a catalogue to check against), so a typed id does not block Save.

Round 2 (lead's review of #693): the words are the console's one refusal reader's (``readRefusal``), so a 401 or a 403 says "Your session has ended" / "Your role does not allow this" and does not
advise typing; a read-only (harness-managed) builder shows the reason and the id as text and takes no typing; the add-step palette and the starters are driven through the real builder to the
node's ``tool_id``; a typed id is put into a starter as what it is; Try again shows it is trying, a new failure is announced again, and one picker per surface announces.

The REAL builder runs in V8 on the strict mini React (``tests/ui/_graph_builder_v8.py`` has the harness); the catalogue request is stubbed, and the REAL ``ApiError`` and ``readRefusal`` of
``ui/foundation/api.js`` read the errors it fails with.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from tests._support.refusal_envelopes import refusal_envelopes
from tests.ui._graph_builder_v8 import DRIVER, PRELUDE, builder_code
from tests.ui._mini_react import mini_react_context
from tests.ui.test_graph_builder_import_shapes import BASE

ROOT = Path(__file__).resolve().parents[2]
PALETTE = (ROOT / "ui" / "components" / "graph-builder" / "gb-palette.jsx").read_text(encoding="utf-8")
STARTERS = (ROOT / "ui" / "components" / "graph-builder" / "gb-starters.jsx").read_text(encoding="utf-8")
INSPECTOR = (ROOT / "ui" / "components" / "graph-builder" / "gb-inspector.jsx").read_text(encoding="utf-8")
BUILDER = (ROOT / "ui" / "components" / "graph-builder" / "graph-builder.jsx").read_text(encoding="utf-8")
API = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")

# the catalogue request is a stub the test can change: its state is ``__cat`` (the REAL ``ApiError`` and ``readRefusal`` of ui/foundation/api.js are loaded, so an error is read as the console reads it)
STUB = """
var __retries = 0;
var __cat = { data: null, error: null, loading: false };
window.primerApi.useResource = function (key) {
  if (key === "tools:catalogue") return { data: __cat.data, error: __cat.error, loading: __cat.loading, refetch: function () { __retries += 1; } };
  return { data: { items: TOOLS } };
};
function refuse(envelope) { return new window.primerApi.ApiError(envelope); }
function byText(root, tag, text) {
  var hit = null;
  (function w(n) {
    if (hit || n == null || typeof n !== 'object') return;
    if (Array.isArray(n)) { n.forEach(w); return; }
    if (!n.__el) return;
    if (n.type === tag && (n.props.children === text || (Array.isArray(n.props.children) && n.props.children.join('') === text))) { hit = n; return; }
    if (typeof n.type === 'function' && n.type !== React.Fragment) w(n.out); else w(n.children);
  })(root);
  return hit;
}
function byLabel(root, label) {
  var hit = null;
  (function w(n) {
    if (hit || n == null || typeof n !== 'object') return;
    if (Array.isArray(n)) { n.forEach(w); return; }
    if (!n.__el) return;
    if (n.props && n.props['aria-label'] === label) { hit = n; return; }
    if (typeof n.type === 'function' && n.type !== React.Fragment) w(n.out); else w(n.children);
  })(root);
  return hit;
}
function draftNodes() { var b = MR.find('gb-builder'); var insp = findType(b, __realInspector) || findType(b, GB_Inspector); return insp ? insp.props.draft.nodes : null; }
function toolIds() { return (draftNodes() || []).filter(function (n) { return n.kind === 'tool_call'; }).map(function (n) { return n.tool_id; }); }
function alerts() { return MR.findAll('gb-tool-catalogue-error').filter(function (el) { return el.props.role === 'alert'; }).length; }
"""
DOWN = STUB + """__cat.error = refuse({ title: "Service Unavailable", status: 503, detail: "the tool service is down" });"""
LOADING = STUB + "__cat.loading = true;"
EMPTY = STUB + "__cat.data = { items: [] };"


def _ctx(prelude: str = ""):
    c = mini_react_context(builder_code(), PRELUDE)
    c.eval(API)
    c.eval(DRIVER)
    c.eval(prelude)
    c.eval("function draftOf() { return inspector().props.draft; }")
    c.eval("function saveDisabled() { return MR.find('gb-save').props.disabled; }")
    return c


@pytest.fixture
def failing():
    c = _ctx(DOWN)
    try:
        yield c
    finally:
        c.close()


def _open(ctx, node: str = "t", *, read_only: bool = False) -> None:
    spec = {**copy.deepcopy(BASE), **({"harness_id": "managed"} if read_only else {})}
    ctx.eval(f"mountBuilder({json.dumps(spec)}); selectNode({json.dumps(node)});")


def _texts(ctx) -> list[str]:
    return json.loads(ctx.eval("JSON.stringify(MR.texts())"))


def _node(ctx, node_id: str = "t") -> dict:
    return json.loads(ctx.eval(f"JSON.stringify(draftOf().nodes.filter(function (n) {{ return n.id === {json.dumps(node_id)}; }})[0])"))


def _fail_with(ctx, envelope: dict) -> None:
    ctx.eval(f"__cat.error = refuse({json.dumps(envelope)}); MR.rerender();")


def _type(ctx, testid: str, value: str, index: int = 0) -> None:
    ctx.eval(f"MR.findAll({json.dumps(testid)})[{index}].props.onChange({{ target: {{ value: {json.dumps(value)} }} }}); MR.rerender();")


def test_a_catalogue_that_cannot_be_loaded_is_not_reported_as_an_empty_search(failing) -> None:
    _open(failing)
    assert failing.eval("MR.find('gb-tool-catalogue-error') !== null") is True, "the picker says the list could not be loaded"
    assert failing.eval("MR.find('gb-tool-catalogue-error').props.role") == "alert"
    texts = " ".join(_texts(failing))
    assert "the tool service is down" in texts, "the server's own words are shown"
    assert "No tools match" not in texts


def test_the_alert_is_the_one_readers_sentence_phrased_for_the_list() -> None:
    """B1: the words come from ``readRefusal`` (``window.primerApi``), not from ``detail || message || title``: a bare code is never shown, and a refusal says its sentence."""
    ctx = _ctx(DOWN)
    try:
        _open(ctx)
        text = " ".join(_texts(ctx))
        assert "The list of tools could not be loaded: the tool service is down." in text, text
        assert "Type the tool's id instead." in text
    finally:
        ctx.close()


@pytest.mark.parametrize(("which", "sentence", "bare_code"), [
    ("session_ended", "Your session has ended; sign in again.", "auth_required"),
    ("role_refused", "Your role does not allow this.", "forbidden_role"),
], ids=["session-ended", "role-refused"])
def test_a_refusal_of_the_session_or_the_role_says_so_in_words_and_does_not_advise_typing(which: str, sentence: str, bare_code: str) -> None:
    """B1: the real 401 and 403 envelopes (tests/_support/refusal_envelopes.py). Typing an id will not help a session that ended, so the advice and the box are not offered; Try again is."""
    ctx = _ctx(STUB)
    try:
        _open(ctx)
        _fail_with(ctx, refusal_envelopes()[which])
        text = " ".join(_texts(ctx))
        assert f"The list of tools could not be loaded: {sentence}" in text, text
        assert f"({bare_code})" not in text and bare_code not in text, "a bare code is not a message"
        assert "Type the tool's id" not in text
        assert ctx.eval("MR.find('gb-tool-id-input') === null") is True
        assert ctx.eval("MR.find('gb-tool-catalogue-retry') !== null") is True
        assert ctx.eval("MR.find('gb-tool-catalogue-error').props.role") == "alert"
    finally:
        ctx.close()


def test_a_read_only_builder_shows_the_reason_and_the_tool_but_takes_no_typing() -> None:
    """B2: a harness-managed (read-only) builder drops every edit, so the Tool id box would swallow what is typed. It shows the alert without the typing advice and the id as text."""
    ctx = _ctx(DOWN)
    try:
        _open(ctx, read_only=True)
        assert ctx.eval("MR.find('gb-tool-catalogue-error') !== null") is True
        text = " ".join(_texts(ctx))
        assert "the tool service is down" in text and "Type the tool's id" not in text
        assert ctx.eval("MR.find('gb-tool-id-input') === null") is True, "no box in a read-only builder"
        assert ctx.eval("MR.find('gb-tool-id-text') !== null") is True and "ts__echo" in " ".join(json.loads(ctx.eval("JSON.stringify(MR.find('gb-tool-id-text').children.map(String))")))
        assert ctx.eval("MR.find('gb-tool-catalogue-retry') !== null") is True, "asking again is not an edit"
    finally:
        ctx.close()


def test_the_tool_id_can_be_typed_and_it_sets_the_steps_tool(failing) -> None:
    _open(failing)
    shown = failing.eval("MR.find('gb-tool-id-input').props.value")
    assert shown == "ts__echo", "the box holds the id the step already has"
    _type(failing, "gb-tool-id-input", " ts__other ")
    assert _node(failing)["tool_id"] == "ts__other", "blanks are not part of an id"
    assert failing.eval("saveDisabled()") is False, "a typed id the builder cannot check does not block Save"


def test_a_tool_id_has_no_blanks_wherever_they_were_typed(failing) -> None:
    """N6: the box drops every blank, inner ones too (a tool id is ``<toolset>__<tool>``; there is no id with a space in it)."""
    _open(failing)
    _type(failing, "gb-tool-id-input", " ts__ot her\t")
    assert _node(failing)["tool_id"] == "ts__other"


def test_the_id_box_is_named_and_the_error_is_described_to_it(failing) -> None:
    _open(failing)
    box = json.loads(failing.eval("JSON.stringify({ label: MR.find('gb-tool-id-input').props['aria-label'], describedby: MR.find('gb-tool-id-input').props['aria-describedby'], errid: MR.find('gb-tool-catalogue-error').props.id })"))
    assert box["label"] == "Tool id"
    assert box["errid"] and box["errid"] in (box["describedby"] or "").split()


def test_the_id_box_suggests_a_real_tool_id(failing) -> None:
    """N5: the placeholder is an id that exists (``workspaces__list_workspace_files``), not one that looks like one."""
    _open(failing)
    assert failing.eval("MR.find('gb-tool-id-input').props.placeholder") == "e.g. workspaces__list_workspace_files"


def test_the_list_can_be_asked_for_again(failing) -> None:
    _open(failing)
    failing.eval("MR.click('gb-tool-catalogue-retry');")
    assert failing.eval("__retries") == 1


def test_asking_again_shows_it_is_trying_and_cannot_be_pressed_twice(failing) -> None:
    """N2: while the request is in flight (the error is still the old one) the button says so and is off."""
    _open(failing)
    assert failing.eval("!!MR.find('gb-tool-catalogue-retry').props.disabled") is False
    assert "Try again" in " ".join(_texts(failing))
    failing.eval("__cat.loading = true; MR.rerender();")
    assert failing.eval("!!MR.find('gb-tool-catalogue-retry').props.disabled") is True
    texts = " ".join(_texts(failing))
    assert "Trying again" in texts and "Try again" not in texts
    assert failing.eval("MR.find('gb-tool-catalogue-error') !== null") is True, "the old words stay until the answer replaces them"


def test_a_second_failure_is_announced_again(failing) -> None:
    """N2: a screen reader announces a ``role=alert`` that is inserted, not one whose text stayed: the alert is keyed on the error, so a new error is a new alert."""
    _open(failing)
    first = failing.eval("MR.find('gb-tool-catalogue-error').key")
    failing.eval("MR.rerender();")
    assert failing.eval("MR.find('gb-tool-catalogue-error').key") == first, "the same error keeps the same alert"
    _fail_with(failing, {"title": "Service Unavailable", "status": 503, "detail": "the tool service is down"})
    assert failing.eval("MR.find('gb-tool-catalogue-error').key") != first


def test_a_tool_step_whose_fields_are_not_known_says_so_and_does_not_ask_to_choose_a_tool(failing) -> None:
    _open(failing)
    texts = _texts(failing)
    assert any("not listed" in s or "no fields" in s.lower() for s in texts), texts
    assert "Choose a tool to see what it needs." not in texts


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
        assert "No tools are available" in texts and "No tools match" not in texts
    finally:
        ctx.close()
    ctx = _ctx(STUB + "__cat.data = { items: TOOLS };")
    try:
        _open(ctx)
        assert ctx.eval("byLabel(MR.find('gb-builder'), 'Search tools') !== null") is True
        ctx.eval("byLabel(MR.find('gb-builder'), 'Search tools').props.onChange({ target: { value: 'zzz-nothing' } }); MR.rerender();")
        assert "No tools match" in " ".join(_texts(ctx))
    finally:
        ctx.close()


def test_the_picker_alone_shows_the_same_for_every_place_it_is_used() -> None:
    """The palette and the starters use the same ``GB_ToolPicker``; mounted directly it answers a failed catalogue the same way."""
    ctx = _ctx(STUB)
    try:
        ctx.eval("var __picked = []; var __retried = 0;")
        ctx.eval("MR.mount(GB_ToolPicker, { tools: [], value: '', onChange: function (v) { __picked.push(v); }, catalogue: { error: { detail: 'boom' }, loading: false, retry: function () { __retried += 1; } } });")
        assert "boom" in " ".join(_texts(ctx)) and ctx.eval("MR.find('gb-tool-id-input') !== null") is True
        _type(ctx, "gb-tool-id-input", "a__b")
        assert json.loads(ctx.eval("JSON.stringify(__picked)")) == ["a__b"]
        ctx.eval("MR.click('gb-tool-catalogue-retry');")
        assert ctx.eval("__retried") == 1
        # a stale list wins over a later error (the list is still good)
        ctx.eval("MR.mount(GB_ToolPicker, { tools: [{ id: 'x__y', description: 'd' }], value: '', onChange: function () {}, catalogue: { error: { detail: 'boom' }, loading: false } });")
        assert ctx.eval("MR.find('gb-tool-id-input') === null") is True and "x__y" in " ".join(_texts(ctx))
    finally:
        ctx.close()


# ---- the three places a tool is picked, through the REAL builder, with the catalogue failing (B3) ----------------------------------------------------------------------------------------

def _empty_graph() -> dict:
    empty = copy.deepcopy(BASE)
    empty["nodes"], empty["edges"] = [], []
    return empty


def _tools_starter(ctx) -> None:
    ctx.eval(f"mountBuilder({json.dumps(_empty_graph())});")
    ctx.eval("MR.findAll('gb-starter').filter(function (el) { return el.props['data-shape'] === 'tools'; })[0].props.onClick(); MR.rerender();")


def _create_from_starter(ctx) -> None:
    ctx.eval("findType(MR.find('gb-starters'), window.EntityPicker).props.onChange('ag-1'); MR.rerender();")
    ctx.eval("byText(MR.find('gb-starters'), 'button', 'Create this graph').props.onClick(); MR.rerender();")


def test_the_add_step_palette_takes_a_typed_tool_id_through_to_the_new_step(failing) -> None:
    failing.eval(f"mountBuilder({json.dumps(copy.deepcopy(BASE))});")
    failing.eval("MR.click('gb-outline-add');")
    failing.eval("MR.findAll('gb-palette-row').filter(function (el) { return el.props['data-purpose'] === 'tool'; })[0].props.onClick(); MR.rerender();")
    assert failing.eval("MR.find('gb-tool-catalogue-error') !== null") is True and failing.eval("MR.find('gb-tool-id-input') !== null") is True
    _type(failing, "gb-tool-id-input", "ts__typed")
    failing.eval("byText(MR.find('gb-palette'), 'button', 'Add step').props.onClick(); MR.rerender();")
    assert failing.eval("MR.find('gb-palette') === null") is True, "the palette closed on Add step"
    assert json.loads(failing.eval("JSON.stringify(toolIds())")).count("ts__typed") == 1


def test_the_tools_starter_takes_a_typed_id_in_each_tool_slot_through_to_the_new_graph(failing) -> None:
    _tools_starter(failing)
    assert failing.eval("MR.findAll('gb-tool-id-input').length") == 2, "both tool slots show the box"
    _type(failing, "gb-tool-id-input", "web__fetch", 0)
    _type(failing, "gb-tool-id-input", "workspaces__write_workspace_file", 1)
    _create_from_starter(failing)
    assert json.loads(failing.eval("JSON.stringify(toolIds())")) == ["web__fetch", "workspaces__write_workspace_file"]


def test_a_typed_id_is_put_into_the_starter_as_what_it_is(failing) -> None:
    """N1: the starter's picks are spliced into JSON text; a quote or a backslash in a typed id must not break the click or change the id."""
    _tools_starter(failing)
    _type(failing, "gb-tool-id-input", 'a"b', 0)
    _type(failing, "gb-tool-id-input", "x\\u0041y", 1)
    assert failing.eval("errOf(function () { findType(MR.find('gb-starters'), window.EntityPicker).props.onChange('ag-1'); MR.rerender(); byText(MR.find('gb-starters'), 'button', 'Create this graph').props.onClick(); MR.rerender(); })") is None
    assert json.loads(failing.eval("JSON.stringify(toolIds())")) == ['a"b', "x\\u0041y"]


def test_one_alert_per_surface_not_one_per_picker(failing) -> None:
    """N4: the starter with two tool slots and the palette drawn over a selected tool step each have two pickers; the failure is announced once."""
    _tools_starter(failing)
    assert failing.eval("MR.findAll('gb-tool-catalogue-error').length") == 2, "both slots say why"
    assert failing.eval("alerts()") == 1
    ctx_2 = _ctx(DOWN)
    try:
        _open(ctx_2, "t")
        ctx_2.eval("MR.click('gb-outline-add');")
        ctx_2.eval("MR.findAll('gb-palette-row').filter(function (el) { return el.props['data-purpose'] === 'tool'; })[0].props.onClick(); MR.rerender();")
        assert ctx_2.eval("MR.findAll('gb-tool-catalogue-error').length") == 2, "the step's picker behind the palette and the palette's"
        assert ctx_2.eval("alerts()") == 1
    finally:
        ctx_2.close()


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
