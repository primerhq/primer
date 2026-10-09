"""A branch condition's value survives being shown and edited (board ticket 01a11e4e-8910, found in the lead's review of #633).

``BranchCondition.value`` is ``Any`` in the model. The builder drew it as ``Array.isArray(v) ? v.join(", ") : String(v)``, so an OBJECT showed as ``[object Object]``, an array of objects as
``[object Object], ...``, and an array under ``is`` as ``1, 2``; and its ``onChange`` ran whatever the text said through ``GR_parseBranchValue``, which falls back to the text itself. So the
first keystroke in the box of a graph that was valid replaced the object by the string ``[object Object]x``: silent data corruption. The text a value is edited as is its JSON when plain
text would not read back to the same value; an edit that is not yet valid JSON leaves a structured value alone; and the input keeps what the operator types while the value is being edited.

``GB_branchValueText``, ``GB_branchValueEdit`` and the input component ``GB_BranchValueInput`` (``gb-branches.jsx``) run here in V8, with the real ``GR_parseBranchValue`` (``graphs.jsx``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

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

    parts = [_slice(GRAPHS, "GR_parseBranchValue")]
    for name in ("GB_branchValueText", "GB_branchValueEdit", "GB_BranchValueInput"):
        start = BRANCHES.find("function " + name + "(")
        if start >= 0:
            parts.append(BRANCHES[start:BRANCHES.index("\n}\n", start) + len("\n}\n")])
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
]   # (no null: an empty box means "no value yet", which reads back as the empty string the "+ condition" button starts with)


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
    assert _js(ctx, 'GB_branchValueText(null, "eq")') == ""


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


@pytest.mark.parametrize(("current", "text", "expected"), [("abc", "abcd", "abcd"), (5, "55", 55), ("x", "true", True), (None, "hello", "hello")])
def test_a_scalar_is_edited_as_before(v8, current, text: str, expected) -> None:
    assert _edit(v8(), text, "eq", current) == {"commit": True, "value": expected}


# ---------------------------------------------------------------------------
# the input: it keeps what is typed while the value is being edited
# ---------------------------------------------------------------------------

_HOST = """
var __commits = [];
function Host(props) { return React.createElement(GB_BranchValueInput, { value: props.value, op: props.op, ariaLabel: "value", onCommit: function (v) { __commits.push(v); } }); }
"""


def _mount(ctx, value, op="eq"):
    ctx.eval(_HOST)
    ctx.eval(f"MR.mount(Host, {json.dumps({'value': value, 'op': op})});")


def _input(ctx):
    return _js(ctx, "(function () { var els = []; var e = MR.find('gb-branch-value'); return e ? { value: e.props.value, invalid: e.props['aria-invalid'] || null, label: e.props['aria-label'] } : null; })()")


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


def test_the_builder_uses_the_input_for_a_conditions_value() -> None:
    assert "<GB_BranchValueInput" in BRANCHES
    assert "Array.isArray(c.value) ? c.value.join" not in BRANCHES, "the lossy rendering is gone"
