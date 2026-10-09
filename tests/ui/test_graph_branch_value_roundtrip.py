"""A branch condition's value survives being shown and edited (board ticket 01a11e4e-8910, found in the lead's review of #633).

``BranchCondition.value`` is ``Any`` in the model. The builder drew it as ``Array.isArray(v) ? v.join(", ") : String(v)``, so an OBJECT showed as ``[object Object]``, an array of objects as
``[object Object], ...``, and an array under ``is`` as ``1, 2``; and its ``onChange`` ran whatever the text said through ``GR_parseBranchValue``, which falls back to the text itself. So the
first keystroke in the box of a graph that was valid replaced the object by the string ``[object Object]x``: silent data corruption. The text a value is edited as is its JSON when plain
text would not read back to the same value; an edit that is not yet valid JSON leaves a structured value alone, SAYS so (a visible alert, and the builder's Save is off until it is fixed); and
the input keeps what the operator types while the value is being edited.

``GB_branchValueText``, ``GB_branchValueEdit`` and the input component ``GB_BranchValueInput`` (``gb-branches.jsx``) run here in V8, with the real ``GR_parseBranchValue`` (``graphs.jsx``).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context
from tests.ui._graph_builder_v8 import DRIVER, PRELUDE, builder_code
from tests.ui.test_graph_builder_import_shapes import BASE

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui" / "components"
BRANCHES = (UI / "graph-builder" / "gb-branches.jsx").read_text(encoding="utf-8")
GRAPHS = (UI / "graphs.jsx").read_text(encoding="utf-8")


def _slice(src: str, name: str) -> str:
    start = src.find("function " + name + "(")
    assert start >= 0, f"{name} is not defined"
    return src[start:src.index("\n}\n", start) + len("\n}\n")]


def _compiled() -> str:
    from primer.api._jsx_bundle import JSXBundler

    for name in ("GB_branchValueText", "GB_branchValueEdit", "GB_BranchValueInput", "GB_BranchBuilder"):
        assert "function " + name + "(" in BRANCHES, f"{name} is not defined in gb-branches.jsx"
    parts = [_slice(GRAPHS, "GR_parseBranchValue"), BRANCHES]
    bundler = JSXBundler(ui_dir=ROOT / "ui", babel_source=(ROOT / "ui" / "vendor" / "babel.min.js").read_text())
    try:
        return bundler._transform("\n".join(parts), "snippet.jsx")
    finally:
        bundler._ctx.close()


@pytest.fixture(scope="module")
def code() -> str:
    return _compiled()


@pytest.fixture
def v8(code):
    made: list = []

    def build():
        ctx = mini_react_context(code)
        made.append(ctx)
        return ctx

    try:
        yield build
    finally:
        for c in made:
            c.close()


def _js(ctx, expression: str):
    return json.loads(ctx.eval("JSON.stringify(" + expression + ")"))


# every kind of value a condition can hold, with the operator it is read under
VALUES = [
    ({"a": 1}, "eq"), ({"a": {"b": [1, 2]}}, "ne"), ([1, 2], "eq"), ([{"a": 1}], "eq"), ([], "eq"), ({}, "eq"),
    (["a", "b"], "in"), (["a", "b"], "not_in"), ([1, 2], "in"), ([1, "a", True], "in"), (["a,b", "c"], "in"), ([" x"], "in"), ([""], "in"), ([{"k": 1}], "in"), ([[1]], "in"),
    ("abc", "eq"), ("", "eq"), ("5", "eq"), ("true", "eq"), ("null", "eq"), ('"quoted"', "eq"), ("[1]", "eq"), ("a b", "ne"),
    (5, "eq"), (5.5, "gt"), (-1, "lt"), (0, "gte"), (True, "eq"), (False, "ne"),
    # a list of strings that LOOK like JSON is not shown in the comma form: "1" would read back as the number 1 (review of #679, B1)
    (["1"], "in"), (["200"], "not_in"), (["true"], "in"), (["null"], "in"), (['"a"'], "in"), (["{}"], "in"), (["[1", "2]"], "in"), (["1", "a"], "in"), ([], "in"), ([], "not_in"),
    # null is not the empty string: it shows as null (an empty box is "no value yet", the empty string)
    (None, "eq"), (None, "ne"),
    # a string with a control character is shown quoted: an <input> strips a newline, so the first edit would have changed the text
    ("a\nb", "eq"), ("tab\there", "eq"), ("a\r\nb", "ne"), ("  ", "eq"),
]


@pytest.mark.parametrize(("value", "op"), VALUES, ids=[f"{json.dumps(v)}-{o}" for v, o in VALUES])
def test_what_the_box_shows_reads_back_as_the_value_it_shows(v8, value, op: str) -> None:
    """The invariant: text(value) parsed again is the same value, whatever its type."""
    ctx = v8()
    text = _js(ctx, f"GB_branchValueText({json.dumps(value)}, {json.dumps(op)})")
    back = _js(ctx, f"GR_parseBranchValue({json.dumps(text)}, {json.dumps(op)})")
    assert back == value and type(back) is type(value), (value, op, text, back)


def test_an_object_shows_as_its_json_and_not_as_object_object(v8) -> None:
    ctx = v8()
    assert _js(ctx, 'GB_branchValueText({"a": 1}, "eq")') == '{"a":1}'
    assert "[object Object]" not in _js(ctx, 'GB_branchValueText([{"a": 1}], "eq")')


def test_a_plain_list_still_reads_as_the_comma_separated_text_the_box_always_showed(v8) -> None:
    ctx = v8()
    assert _js(ctx, 'GB_branchValueText(["a", "b"], "in")') == "a, b"
    assert _js(ctx, 'GB_branchValueText("abc", "eq")') == "abc"
    assert _js(ctx, 'GB_branchValueText(5, "eq")') == "5"
    assert _js(ctx, 'GB_branchValueText(undefined, "eq")') == "", "no value yet is an empty box"
    assert _js(ctx, 'GB_branchValueText(null, "eq")') == "null", "null is not the empty string"
    assert _js(ctx, 'GB_branchValueText([], "in")') == "", "an empty list under is one of reads back from an empty box"


@pytest.mark.parametrize("text", ["a\nb", "tab\there", "a\r\nb"])
def test_a_string_with_a_control_character_is_shown_quoted(v8, text: str) -> None:
    """An ``<input>`` strips a newline, so the first keystroke in a box that showed ``a<newline>b`` as it is stored ``ab``; quoted, it is edited as JSON and keeps the character."""
    assert _js(v8(), f"GB_branchValueText({json.dumps(text)}, 'eq')") == json.dumps(text)


@pytest.mark.parametrize("member", ["1", "200", "true", "null", '"a"', "{}", "[]", "1e3"])
def test_a_list_whose_text_would_read_back_as_another_type_is_shown_as_json(v8, member: str) -> None:
    """B1 of the review: the comma form is only for a list that reads back, member for member, the same."""
    text = _js(v8(), f'GB_branchValueText([{json.dumps(member)}], "in")')
    assert text == json.dumps([member], separators=(",", ":")), text


def test_a_list_of_words_that_read_back_the_same_keeps_the_comma_form_even_when_one_word_is_half_a_json_text(v8) -> None:
    """``["[1"]`` is not JSON as text, so the comma form reads back as the same one-member list."""
    assert _js(v8(), 'GB_branchValueText(["[1"], "in")') == "[1"


def _edit(ctx, text: str, op: str, current):
    return _js(ctx, f"GB_branchValueEdit({json.dumps(text)}, {json.dumps(op)}, {json.dumps(current)})")


def test_a_keystroke_that_is_not_json_leaves_an_object_alone(v8) -> None:
    """The ticket: seed {"a": 1}, type one character."""
    assert _edit(v8(), '{"a":1}x', "eq", {"a": 1})["commit"] is False


def test_an_edit_that_is_valid_json_replaces_the_object(v8) -> None:
    got = _edit(v8(), '{"a":2}', "eq", {"a": 1})
    assert got == {"commit": True, "value": {"a": 2}}


def test_a_list_under_an_operator_that_takes_one_value_is_kept_until_the_text_is_json(v8) -> None:
    ctx = v8()
    assert _edit(ctx, "[1, 2", "eq", [1, 2])["commit"] is False
    assert _edit(ctx, "[1, 3]", "eq", [1, 3]) == {"commit": True, "value": [1, 3]}


def test_the_comma_list_of_is_one_of_still_takes_any_text(v8) -> None:
    got = _edit(v8(), "a, b, c", "in", ["a", "b"])
    assert got == {"commit": True, "value": ["a", "b", "c"]}


def test_clearing_the_box_is_a_deliberate_edit_and_commits_the_empty_string(v8) -> None:
    assert _edit(v8(), "", "eq", {"a": 1}) == {"commit": True, "value": ""}


@pytest.mark.parametrize(("current", "op", "text"), [([{"a": 1}], "in", '[{"a":1}]x'), ([1, 2], "in", "[1,2"), ([1, 2], "not_in", "1, 2"), (["1"], "in", '["1"')])
def test_a_list_shown_as_json_is_kept_until_the_text_is_json_too(v8, current, op: str, text: str) -> None:
    """B2 of the review: only a list SHOWN in the comma form takes any text. One shown as JSON (objects, numbers, JSON-looking strings) is as much a structured value as an object is."""
    assert _edit(v8(), text, op, current) == {"commit": False}


def test_a_list_shown_in_the_comma_form_and_an_empty_one_take_any_text(v8) -> None:
    ctx = v8()
    assert _edit(ctx, "a, b, [", "in", ["a", "b"]) == {"commit": True, "value": ["a", "b", "["]}
    assert _edit(ctx, "a", "in", []) == {"commit": True, "value": ["a"]}, "an empty list takes the first word typed into its empty box"


def test_whitespace_only_text_over_a_structured_value_clears_it_and_does_not_store_the_blanks(v8) -> None:
    ctx = v8()
    assert _edit(ctx, "   ", "eq", {"a": 1}) == {"commit": True, "value": ""}
    assert _edit(ctx, "   ", "in", [{"a": 1}]) == {"commit": True, "value": []}
    assert _edit(ctx, "  ", "eq", "x") == {"commit": True, "value": "  "}, "a string stays whatever is typed"


def test_null_typed_over_an_object_is_valid_json_and_stores_null(v8) -> None:
    assert _edit(v8(), "null", "eq", {"a": 1}) == {"commit": True, "value": None}


@pytest.mark.parametrize(("current", "text", "expected"), [("abc", "abcd", "abcd"), (5, "55", 55), ("x", "true", True), (None, "hello", "hello")])
def test_a_scalar_is_edited_as_before(v8, current, text: str, expected) -> None:
    assert _edit(v8(), text, "eq", current) == {"commit": True, "value": expected}


# ---------------------------------------------------------------------------
# the input: it keeps what is typed while the value is being edited
# ---------------------------------------------------------------------------

_HOST = """
var __commits = [];
var __json_errors = [];
function Host(props) {
  return React.createElement(GB_BranchValueInput, {
    value: props.value, op: props.op, ariaLabel: "value", errorKey: "bv:0:0:0",
    onJsonError: function (key, err) { __json_errors.push([key, err]); },
    onCommit: function (v) { __commits.push(v); },
  });
}
function Blank() { return null; }
"""


def _mount(ctx, value, op="eq"):
    ctx.eval(_HOST)
    ctx.eval(f"MR.mount(Host, {json.dumps({'value': value, 'op': op})});")


def _input(ctx):
    return _js(ctx, "(function () { var e = MR.find('gb-branch-value'); return e ? { value: e.props.value, invalid: e.props['aria-invalid'] || null, label: e.props['aria-label'], describedby: e.props['aria-describedby'] || null } : null; })()")


def _type(ctx, text: str):
    ctx.eval(f"MR.find('gb-branch-value').props.onChange({{ target: {{ value: {json.dumps(text)} }} }}); MR.rerender();")


def test_the_input_shows_an_object_as_json_with_its_name(v8) -> None:
    ctx = v8()
    _mount(ctx, {"a": 1})
    shown = _input(ctx)
    assert shown["value"] == '{"a":1}' and shown["label"] == "value", shown


def test_typing_into_an_object_stores_nothing_until_the_text_is_json_and_keeps_the_text(v8) -> None:
    ctx = v8()
    _mount(ctx, {"a": 1})
    _type(ctx, '{"a":1}x')
    assert _js(ctx, "__commits") == [], "the first keystroke replaced the object"
    shown = _input(ctx)
    assert shown["value"] == '{"a":1}x', "what the operator typed stays in the box"
    assert shown["invalid"] == "true", "and the box says it is not JSON"
    _type(ctx, '{"a":12}')
    assert _js(ctx, "__commits") == [{"a": 12}]
    assert _input(ctx)["invalid"] is None


def test_typing_into_a_string_commits_each_keystroke_as_it_always_did(v8) -> None:
    ctx = v8()
    _mount(ctx, "ab")
    _type(ctx, "abc")
    assert _js(ctx, "__commits") == ["abc"]


def test_text_on_the_way_to_a_number_is_not_rewritten_under_the_cursor(v8) -> None:
    """``5.`` is not JSON, so it is stored as the text it is (as it always was) and the box must keep ``5.`` and not snap to something else while the next digit is typed."""
    ctx = v8()
    _mount(ctx, 4)
    _type(ctx, "5.")
    assert _input(ctx)["value"] == "5.", _input(ctx)
    assert _js(ctx, "__commits") == ["5."]
    ctx.eval("MR.rerender({ value: '5.', op: 'eq' });")
    assert _input(ctx)["value"] == "5."
    _type(ctx, "5.5")
    assert _js(ctx, "__commits") == ["5.", 5.5]


def test_a_value_changed_from_outside_replaces_the_text(v8) -> None:
    """An undo, or the same box reused for another condition: the box shows the stored value again."""
    ctx = v8()
    _mount(ctx, {"a": 1})
    _type(ctx, "garbage")
    ctx.eval("MR.rerender({ value: { b: 2 }, op: 'eq' });")
    assert _input(ctx)["value"] == '{"b":2}'


def test_changing_the_operator_redraws_the_same_value_in_that_operators_form(v8) -> None:
    ctx = v8()
    _mount(ctx, ["a", "b"], op="in")
    assert _input(ctx)["value"] == "a, b"
    ctx.eval("MR.rerender({ value: ['a', 'b'], op: 'eq' });")
    assert _input(ctx)["value"] == '["a","b"]'


def _alert(ctx):
    return _js(ctx, "(function () { var e = MR.find('gb-branch-value-error'); return e ? { id: e.props.id, role: e.props.role, text: MR.texts().join(' ') } : null; })()")


def test_a_value_that_is_not_json_yet_says_so_in_an_alert_the_box_is_described_by(v8) -> None:
    """B3 of the review: aria-invalid alone is invisible (nothing styles it) and the typed text would be silently lost on Save."""
    ctx = v8()
    _mount(ctx, {"a": 1})
    assert _alert(ctx) is None, "no alert while the box shows what is stored"
    _type(ctx, '{"a":1}x')
    alert = _alert(ctx)
    assert alert and alert["role"] == "alert", alert
    assert "Not JSON yet" in alert["text"] and "stored value is unchanged" in alert["text"], alert["text"]
    assert alert["id"] and alert["id"] in (_input(ctx)["describedby"] or "").split(), "the box is described by the alert"
    _type(ctx, '{"a":2}')
    assert _alert(ctx) is None and alert["id"] not in (_input(ctx)["describedby"] or "").split(), "fixed: the alert goes, and the box is no longer described by it"


def test_the_input_reports_its_invalid_state_to_the_builders_save_gate_and_clears_it(v8) -> None:
    """The builder's ``onJsonError`` (``graph-builder.jsx``) keeps the keys with an error and Save is off while there is one (``GR_JsonField`` does the same on blur)."""
    ctx = v8()
    _mount(ctx, {"a": 1})
    _type(ctx, '{"a":1}x')
    reports = _js(ctx, "__json_errors")
    assert reports and reports[-1][0] == "bv:0:0:0" and isinstance(reports[-1][1], str) and reports[-1][1], reports
    _type(ctx, '{"a":2}')                                              # cleared on a commit (the builder re-renders the box with what was stored)
    ctx.eval("MR.rerender({ value: { a: 2 }, op: 'eq' });")
    assert _js(ctx, "__json_errors")[-1] == ["bv:0:0:0", None]
    _type(ctx, "garbage")
    assert _js(ctx, "__json_errors")[-1][1]
    ctx.eval("MR.rerender({ value: { b: 3 }, op: 'eq' });")            # cleared when the stored value changes from outside (an undo)
    assert _js(ctx, "__json_errors")[-1] == ["bv:0:0:0", None]
    assert _input(ctx)["value"] == '{"b":3}'
    _type(ctx, "garbage again")
    assert _js(ctx, "__json_errors")[-1][1]
    ctx.eval("MR.mount(Blank, {});")                                   # and cleared when the box goes away (another step selected)
    assert _js(ctx, "__json_errors")[-1] == ["bv:0:0:0", None]


def test_a_valid_edit_never_reports_an_error(v8) -> None:
    ctx = v8()
    _mount(ctx, "ab")
    _type(ctx, "abc")
    assert all(err is None for _key, err in _js(ctx, "__json_errors"))


@pytest.mark.parametrize(("typed", "committed"), [('{"a": 2}', {"a": 2}), ("[1,  2]", [1, 2])])
def test_a_spelling_that_is_not_the_canonical_text_stays_as_typed(v8, typed: str, committed) -> None:
    """The caret: ``{"a": 2}`` is stored as the object and its canonical text is ``{"a":2}``; the box must not be rewritten to that under the cursor."""
    ctx = v8()
    _mount(ctx, {"a": 1})
    _type(ctx, typed)
    assert _js(ctx, "__commits") == [committed]
    ctx.eval(f"MR.rerender({{ value: {json.dumps(committed)}, op: 'eq' }});")   # the builder re-renders with what was committed
    assert _input(ctx)["value"] == typed and _input(ctx)["invalid"] is None


@pytest.mark.parametrize(("typed", "committed"), [("1e3", 1000), ("5.0", 5), ("0x10", "0x10"), ("05", "05")])
def test_a_number_spelled_another_way_stays_as_typed(v8, typed: str, committed) -> None:
    ctx = v8()
    _mount(ctx, 4)
    _type(ctx, typed)
    assert _js(ctx, "__commits") == [committed]
    ctx.eval(f"MR.rerender({{ value: {json.dumps(committed)}, op: 'eq' }});")
    assert _input(ctx)["value"] == typed


def test_the_builder_uses_the_input_for_a_conditions_value() -> None:
    assert "<GB_BranchValueInput" in BRANCHES
    assert "Array.isArray(c.value) ? c.value.join" not in BRANCHES, "the lossy rendering is gone"


# ---------------------------------------------------------------------------
# the real builder: the Save gate, the condition wiring, the identity of the box, the help line
# ---------------------------------------------------------------------------

def _spec() -> dict:
    """BASE with the conditional edge of step ``a`` holding an object, a number, and a list of objects under ``is one of``."""
    spec = copy.deepcopy(BASE)
    spec["edges"][1]["router"]["branches"] = [
        {"conditions": [{"path": "ok", "op": "eq", "value": {"a": 1}}, {"path": "n", "op": "gt", "value": 3}], "to_node": "f"},
        {"conditions": [{"path": "ok", "op": "in", "value": [{"k": 1}]}], "to_node": "t"},
    ]
    # a second conditional edge (from step ``t``) holding the very same object: the same canonical text under another edge
    spec["edges"].append({"kind": "conditional", "from_node": "t", "router": {"kind": "json_path", "branches": [{"conditions": [{"path": "ok", "op": "eq", "value": {"a": 1}}], "to_node": "e"}], "default_to": "e"}})
    return spec


@pytest.fixture
def builder():
    c = mini_react_context(builder_code(), PRELUDE)
    c.eval(DRIVER)
    c.eval("function draftOf() { return inspector().props.draft; }")
    c.eval("function boxes() { return MR.findAll('gb-branch-value').filter(function (el) { return el.type === 'input'; }); }")   # not the alert span, whose test id starts with the same words
    c.eval("function typeInto(i, text) { boxes()[i].props.onChange({ target: { value: text } }); MR.rerender(); }")
    c.eval("function saveDisabled() { return MR.find('gb-save').props.disabled; }")
    try:
        yield c
    finally:
        c.close()


def _open(ctx, spec: dict) -> None:
    ctx.eval(f"mountBuilder({json.dumps(spec)}); selectNode('a');")


def _shown(ctx) -> list[str]:
    return json.loads(ctx.eval("JSON.stringify(boxes().map(function (b) { return b.props.value; }))"))


def _conditions(ctx, branch: int) -> list[dict]:
    return json.loads(ctx.eval(f"JSON.stringify(draftOf().edges[1].router.branches[{branch}].conditions)"))


def test_the_builders_boxes_show_each_kind_of_value_as_json_where_text_would_not_read_back(builder) -> None:
    _open(builder, _spec())
    assert _shown(builder) == ['{"a":1}', "3", '[{"k":1}]']


def test_an_edit_that_is_not_json_yet_turns_the_builders_save_off_and_a_fix_turns_it_on(builder) -> None:
    """B3: with another edit pending, Save used to PUT the old value and lose the typed text; now Save is off and the alert says why, until the text is JSON or the box is cleared."""
    _open(builder, _spec())
    builder.eval("typeInto(1, '4');")                                   # an ordinary edit: the graph is dirty and may be saved
    assert builder.eval("saveDisabled()") is False
    builder.eval("typeInto(0, '{\"a\":1}x');")
    assert builder.eval("saveDisabled()") is True, "Save must wait for the half-typed JSON"
    assert builder.eval("MR.find('gb-branch-value-error') !== null") is True
    assert _conditions(builder, 0)[0]["value"] == {"a": 1}, "and the stored value is untouched"
    builder.eval("typeInto(0, '{\"a\":2}');")
    assert builder.eval("saveDisabled()") is False and builder.eval("MR.find('gb-branch-value-error') === null") is True
    assert _conditions(builder, 0)[0]["value"] == {"a": 2}
    builder.eval("typeInto(2, '[{\"k\":1}]x');")                      # a list of objects under "is one of" is guarded too (B2, in the real builder)
    assert builder.eval("saveDisabled()") is True and _conditions(builder, 1)[0]["value"] == [{"k": 1}]
    builder.eval("typeInto(2, '');")                                    # clearing the box is the way out
    assert builder.eval("saveDisabled()") is False and _conditions(builder, 1)[0]["value"] == []


def test_selecting_another_step_drops_the_half_typed_text_and_the_save_gate_with_it(builder) -> None:
    _open(builder, _spec())
    builder.eval("typeInto(1, '4'); typeInto(0, '{\"a\":1}x');")
    assert builder.eval("saveDisabled()") is True
    builder.eval("selectNode('t');")
    assert builder.eval("saveDisabled()") is False, "the box that held the error is gone"


def test_a_value_typed_into_the_second_condition_changes_that_condition_and_no_other(builder) -> None:
    """Pins ``j === ci`` in the commit's ``conditions.map``."""
    _open(builder, _spec())
    builder.eval("typeInto(1, '7');")
    first, second = _conditions(builder, 0)
    assert first["value"] == {"a": 1} and second["value"] == 7, (first, second)


def test_the_value_box_is_keyed_by_its_edge_branch_and_condition_so_another_edge_does_not_inherit_its_half_typed_text(builder) -> None:
    """The edge inspector reuses ONE ``GB_BranchBuilder`` for whichever edge is selected. Two edges holding the same object share a canonical text, so a box that kept its state across them
    showed the first edge's unsaved garbage under the second."""
    spec = _spec()
    builder.eval(f"var __draft = GB_reducer({json.dumps(BASE)}, {{ type: 'IMPORT_SPEC', spec: {json.dumps(spec)} }});")
    builder.eval("MR.mount(GB_Inspector, inspectorProps(__draft, null, 1));")
    assert _shown(builder)[0] == '{"a":1}'
    builder.eval("typeInto(0, '{\"a\":1}x');")
    assert _shown(builder)[0] == '{"a":1}x'
    builder.eval("MR.rerender(inspectorProps(__draft, null, 6));")
    assert _shown(builder) == ['{"a":1}'], "the other edge shows its own value"
    assert builder.eval("MR.find('gb-branch-value-error') === null") is True


def test_the_edge_inspector_and_the_steps_inspector_both_report_a_half_typed_value_to_the_save_gate(builder) -> None:
    """``GB_Inspector`` renders ``GB_BranchBuilder`` twice (the selected edge, and a step's "What happens next"); each must pass the builder's ``onJsonError`` down, or Save ignores that box."""
    builder.eval(f"var __draft = GB_reducer({json.dumps(BASE)}, {{ type: 'IMPORT_SPEC', spec: {json.dumps(_spec())} }}); var __errs = [];")
    builder.eval("function props(node, edge) { var p = inspectorProps(__draft, node, edge); p.onJsonError = function (k, e) { __errs.push([k, e]); }; return p; }")
    builder.eval("MR.mount(GB_Inspector, props(null, 1)); typeInto(0, '{\"a\":1}x');")
    assert json.loads(builder.eval("JSON.stringify(__errs)"))[-1][0] == "bv:1:0:0", "the edge inspector's box did not report"
    builder.eval("__errs.length = 0; MR.mount(GB_Inspector, props(__draft.nodes.filter(function (n) { return n.id === 'a'; })[0], null)); typeInto(0, '{\"a\":1}x');")
    reports = json.loads(builder.eval("JSON.stringify(__errs)"))
    assert reports and reports[-1][0] == "bv:1:0:0" and reports[-1][1], "the step inspector's box did not report"


def test_a_help_line_says_what_the_box_takes(builder) -> None:
    _open(builder, _spec())
    assert any("1 is a number" in s and "is text" in s for s in json.loads(builder.eval("JSON.stringify(MR.texts())"))), "the value box has no help line"
