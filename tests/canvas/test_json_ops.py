"""JSON ops: the alternative input to Canvas Lang, and the normalized form records keep."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from primer.canvas.lang import Append, Assign, Clear, Delete, Ref, SetProp, parse_program
from primer.canvas.ops import (
    MAX_OPS_CHARS,
    MAX_SOURCE_CHARS,
    OP_SHAPES,
    AppendOp,
    CanvasOp,
    ClearOp,
    DeleteOp,
    PropOp,
    SetOp,
    ensure_ops_fit,
    ops_errors,
    ops_to_statements,
    statements_to_ops,
)
from primer.canvas.state import CanvasState, Limits, apply_batch

GOLDEN = Path(__file__).parent / "golden"
S = CanvasState.empty()
L = Limits()
OPS = TypeAdapter(list[CanvasOp])
SELECT_SIG = "Select(label, options, value?, multiple?, wake?)"


def G(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8")


CREATE_OPS = [
    {"op": "set", "id": "root", "type": "Column", "children": ["title", "cover", "controls", "player", "more", "viz"]},
    {"op": "set", "id": "title", "type": "Text", "props": {"text": "Pick a playlist", "style": "h2"}},
    {"op": "set", "id": "cover", "type": "Image", "props": {"src": "https://cdn.example.com/covers/kind-of-blue.jpg", "alt": "Album cover"}},
    {"op": "set", "id": "controls", "type": "Row", "children": ["genre", "play"]},
    {"op": "set", "id": "genre", "type": "Select", "props": {"label": "Genre", "options": ["Jazz", "Lo-fi", "Classical"], "value": "Jazz"}},
    {"op": "set", "id": "play", "type": "Button", "props": {"label": "Play", "style": "primary"}},
    {"op": "set", "id": "player", "type": "Audio", "props": {"src": "https://cdn.example.com/audio/so-what.mp3", "title": "So What - Miles Davis"}},
    {"op": "set", "id": "more", "type": "Link", "props": {"label": "Open on Bandcamp", "href": "https://milesdavis.bandcamp.com/album/kind-of-blue"}},
    {"op": "set", "id": "viz", "type": "Html", "props": {"html": "<div id=b></div><script>b.textContent='3 tracks queued'</script>", "height": 60}},
]

UPDATE_OPS = [
    {"op": "prop", "id": "cover", "prop": "src", "value": "https://cdn.example.com/covers/lofi-beats.jpg"},
    {"op": "set", "id": "player", "type": "Audio", "props": {"src": "https://cdn.example.com/audio/rainy-day.mp3", "title": "Rainy Day - Lo-fi Beats"}},
    {"op": "prop", "id": "play", "prop": "disabled", "value": True},
    {"op": "set", "id": "skip", "type": "Button", "props": {"label": "Skip"}},
    {"op": "append", "id": "controls", "children": ["skip"]},
]


def _ops(raw):
    return OPS.validate_python(raw)


def test_json_ops_give_the_same_canvas_as_the_program():
    from_lang = apply_batch(S, parse_program(G("playlist_create.cl")), limits=L)
    from_ops = apply_batch(S, ops_to_statements(_ops(CREATE_OPS)), limits=L, unit="op")
    assert from_ops.errors == () and from_ops.state == from_lang.state and from_ops.changed == from_lang.changed
    update_lang = apply_batch(from_lang.state, parse_program(G("playlist_update.cl")), limits=L)
    update_ops = apply_batch(from_ops.state, ops_to_statements(_ops(UPDATE_OPS)), limits=L, unit="op")
    assert update_ops.errors == () and update_ops.state == update_lang.state and update_ops.changed == update_lang.changed


def test_op_errors_say_op_n():
    ops = _ops([
        {"op": "set", "id": "a", "type": "Text", "props": {"text": "x"}},
        {"op": "set", "id": "b", "type": "Select", "props": {"label": "Genre", "options": "Jazz"}},
        {"op": "append", "id": "root", "children": ["a", "b"]},
    ])
    r = apply_batch(S, ops_to_statements(ops), limits=L, unit="op")
    assert r.state is None
    assert [e.render() for e in r.errors] == [f"op 2: Select needs options as a list ({SELECT_SIG})"]


def test_ops_are_judged_by_the_same_rules_as_a_program():
    cases = [
        ({"op": "set", "id": "Bad Id", "type": "Text", "props": {"text": "x"}}, "op 1: 'Bad Id' is not an element id (a lowercase letter, then lowercase letters, digits or _, at most 32)"),
        ({"op": "set", "id": "a", "type": "Buton"}, "op 1: unknown element type 'Buton' (Column, Row, Card, Divider, Text, Button, Select, Image, Audio, Link, Html)"),
        ({"op": "set", "id": "a", "type": "Column", "props": {"gap": 4}}, "op 1: Column needs children as a list (Column(children, gap?, align?))"),
        ({"op": "set", "id": "a", "type": "Text", "props": {"text": "x"}, "children": ["b"]}, "op 1: Text has no property 'children' (Text(text, style?); every element also takes hidden, disabled)"),
        ({"op": "set", "id": "a", "type": "Button", "props": {"label": "Go", "colour": "red"}}, "op 1: Button has no property 'colour' (Button(label, style?, disabled?, wake?); every element also takes hidden, disabled)"),
        ({"op": "set", "id": "a", "type": "Text", "props": {"text": 5}}, "op 1: text must be text (Text(text, style?))"),
        ({"op": "set", "id": "a", "type": "Button", "props": {"label": "Go", "disabled": 1}}, "op 1: disabled must be true or false (Button(label, style?, disabled?, wake?))"),
        ({"op": "set", "id": "root", "type": "Row", "children": []}, "op 1: root is always a Column"),
        ({"op": "append", "id": "nope", "children": ["a"]}, "op 1: no element named 'nope'"),
        ({"op": "delete", "ids": ["nope"]}, "op 1: no element named 'nope'"),
        ({"op": "delete", "ids": ["root"]}, "op 1: root cannot be deleted; use clear to empty the canvas"),
        ({"op": "prop", "id": "nope", "prop": "x", "value": 1}, "op 1: no element named 'nope'"),
    ]
    for raw, message in cases:
        r = apply_batch(S, ops_to_statements(_ops([raw])), limits=L, unit="op")
        assert [e.render() for e in r.errors] == [message], raw


@pytest.mark.parametrize("sign", ["", "-"])
@pytest.mark.parametrize("digits", [30, 400])
def test_an_integer_beyond_the_range_of_a_float_is_a_line_error_not_a_crash(digits, sign):
    # JSON allows an integer of any length, and 400 digits is more than a float holds: apply_batch must answer it, not raise
    raw = '[{"op":"set","id":"root","type":"Column","props":{"gap":' + sign + "9" * digits + '},"children":[]}]'
    r = apply_batch(S, ops_to_statements(OPS.validate_json(raw)), limits=L, unit="op")
    assert r.state is None
    assert [e.render() for e in r.errors] == ["op 1: gap must be a number from 0 to 64 (Column(children, gap?, align?))"]


def test_a_json_null_removes_a_property_and_children_can_be_replaced():
    built = apply_batch(S, ops_to_statements(_ops(CREATE_OPS)), limits=L, unit="op").state
    off = apply_batch(built, ops_to_statements(_ops([{"op": "prop", "id": "play", "prop": "disabled", "value": True}])), limits=L, unit="op").state
    assert off.elements["play"]["props"]["disabled"] is True
    on = apply_batch(off, ops_to_statements(_ops([{"op": "prop", "id": "play", "prop": "disabled", "value": None}])), limits=L, unit="op").state
    assert "disabled" not in on.elements["play"]["props"]
    reordered = apply_batch(built, ops_to_statements(_ops([{"op": "prop", "id": "controls", "prop": "children", "value": ["play", "genre"]}])), limits=L, unit="op")
    assert reordered.errors == () and reordered.state.elements["controls"]["children"] == ["play", "genre"]


def test_the_op_models_validate_their_shape():
    ops = _ops([
        {"op": "set", "id": "a", "type": "Text"},
        {"op": "prop", "id": "a", "prop": "style", "value": None},
        {"op": "append", "id": "root", "children": ["a"]},
        {"op": "delete", "ids": ["a"]},
        {"op": "clear"},
    ])
    assert [type(op) for op in ops] == [SetOp, PropOp, AppendOp, DeleteOp, ClearOp]
    assert ops[0].props == {} and ops[0].children is None
    for bad in (
        {"op": "explode"},
        {"op": "set", "id": "a"},
        {"op": "set", "id": "a", "type": "Text", "propz": {}},
        {"op": "set", "id": 5, "type": "Text"},
        {"op": "set", "id": "a", "type": "Text", "children": "b"},
        {"op": "prop", "id": "a", "prop": "x"},
        {"op": "append", "id": "root", "children": [1]},
        {"op": "delete", "ids": "a"},
        {"op": "clear", "all": True},
        {"id": "a"},
    ):
        with pytest.raises(ValidationError):
            OPS.validate_python([bad])


def test_ops_become_statements_numbered_from_one():
    stmts = ops_to_statements(_ops([
        {"op": "set", "id": "col", "type": "Column", "props": {"gap": 4}, "children": ["a", "b"]},
        {"op": "set", "id": "t", "type": "Text", "props": {"text": "x"}},
        {"op": "prop", "id": "t", "prop": "style", "value": "h1"},
        {"op": "append", "id": "col", "children": ["c"]},
        {"op": "delete", "ids": ["a", "b"]},
        {"op": "clear"},
    ]))
    assert stmts == [
        Assign(1, "col", "Column", ([Ref("a"), Ref("b")],), (("gap", 4),)),
        Assign(2, "t", "Text", (), (("text", "x"),)),
        SetProp(3, "t", "style", "h1"),
        Append(4, "col", ("c",)),
        Delete(5, ("a", "b")),
        Clear(6),
    ]
    assert ops_to_statements([]) == []


def test_a_container_without_children_and_a_non_container_with_children():
    no_children = ops_to_statements(_ops([{"op": "set", "id": "c", "type": "Row"}]))
    assert no_children == [Assign(1, "c", "Row", (), ())]
    odd = ops_to_statements(_ops([{"op": "set", "id": "t", "type": "Text", "props": {"text": "x"}, "children": ["a"]}]))
    assert odd == [Assign(1, "t", "Text", (), (("text", "x"), ("children", [Ref("a")])))]
    prop = ops_to_statements(_ops([{"op": "prop", "id": "c", "prop": "children", "value": ["a", "b"]}]))
    assert prop == [SetProp(1, "c", "children", [Ref("a"), Ref("b")])]
    not_ids = ops_to_statements(_ops([{"op": "prop", "id": "c", "prop": "children", "value": ["a", 2]}]))
    assert not_ids == [SetProp(1, "c", "children", ["a", 2])]


def test_statements_to_ops_normalizes():
    stmts = parse_program(
        'controls = Row([genre, play])\n'
        'genre = Select("Genre", ["Jazz"], "Jazz")\n'
        'play = Button("Play", "secondary", wake=true)\n'
        'viz = Html(..., 80)\n'
        'cover.src = "https://cdn.example.com/a.png"\n'
        'play.label = ...\n'
        'controls.children = [play, genre]\n'
        'controls += [skip]\n'
        'del a, b\n'
        'clear\n'
    )
    ops = statements_to_ops(stmts)
    assert ops == [
        {"op": "set", "id": "controls", "type": "Row", "props": {}, "children": ["genre", "play"]},
        {"op": "set", "id": "genre", "type": "Select", "props": {"label": "Genre", "options": ["Jazz"], "value": "Jazz"}},
        {"op": "set", "id": "play", "type": "Button", "props": {"label": "Play"}},
        {"op": "set", "id": "viz", "type": "Html", "props": {"height": 80}},
        {"op": "prop", "id": "cover", "prop": "src", "value": "https://cdn.example.com/a.png"},
        {"op": "prop", "id": "controls", "prop": "children", "value": ["play", "genre"]},
        {"op": "append", "id": "controls", "children": ["skip"]},
        {"op": "delete", "ids": ["a", "b"]},
        {"op": "clear"},
    ]
    assert json.loads(json.dumps(ops)) == ops
    # positional booleans and named ones come out the same way
    assert statements_to_ops(parse_program('b = Button("Go", "primary", true)')) == [
        {"op": "set", "id": "b", "type": "Button", "props": {"label": "Go", "style": "primary", "disabled": True}}
    ]


def test_normalized_ops_apply_to_the_same_canvas_and_do_not_share_the_statements_data():
    stmts = parse_program(G("playlist_create.cl"))
    ops = statements_to_ops(stmts)
    direct = apply_batch(S, stmts, limits=L)
    via_ops = apply_batch(S, ops_to_statements(_ops(ops)), limits=L, unit="op")
    assert via_ops.errors == () and via_ops.state == direct.state
    ops[4]["props"]["options"].append("mutated")
    assert stmts[4].args[1] == ["Jazz", "Lo-fi", "Classical"]


@pytest.mark.parametrize("program", ['a = Buton("x")', 'a = Text("x", "h1", "extra")', 'a = Text("x", colour="red")'])
def test_statements_that_cannot_be_named_are_a_value_error(program):
    with pytest.raises(ValueError):
        statements_to_ops(parse_program(program))


def test_statements_to_ops_refuses_what_is_not_a_statement():
    with pytest.raises(TypeError):
        statements_to_ops([object()])


# ---- the size of a batch (pivot P5, Task 9: the lead's ruling after S2) ----------------------------------------------------------------


def test_the_caps_of_the_two_inputs_are_the_same():
    assert MAX_SOURCE_CHARS == MAX_OPS_CHARS == 600_000


def _clears(n: int) -> list[dict]:
    return [{"op": "clear"}] * n


def _compact(ops: object) -> int:
    return len(json.dumps(ops, separators=(",", ":"), ensure_ascii=False))


def _ops_of_size(n: int) -> list[dict]:
    """One ``prop`` op whose string value is padded so that the list is EXACTLY ``n`` characters of compact JSON."""
    bare = {"op": "prop", "id": "a", "prop": "b", "value": ""}
    ops = [{**bare, "value": "x" * (n - _compact([bare]))}]
    assert _compact(ops) == n
    return ops


def test_ops_of_exactly_the_cap_fit_and_one_more_does_not():
    """The boundary itself: 600000 characters are accepted and 600001 are refused (a ``>=`` or an off-by-one in the check fails here)."""
    at_the_cap = _ops_of_size(MAX_OPS_CHARS)
    assert ensure_ops_fit(at_the_cap) is at_the_cap
    with pytest.raises(PydanticCustomError) as exc:
        ensure_ops_fit(_ops_of_size(MAX_OPS_CHARS + 1))
    assert exc.value.type == "ops_too_large"
    assert str(exc.value) == "ops hold 600001 characters of JSON; the limit is 600000"


def test_a_list_of_many_small_ops_is_counted_whole_and_compact():
    """The size is that of the whole list in compact form: not of the ops alone, and not with the spaces ``json.dumps`` writes by default."""
    bare = {"op": "prop", "id": "a", "prop": "b", "value": "x" * 555}
    # 1000 ops of 599 chars + 999 commas + 2 brackets = 600,001 chars; shrinking the last op by 2 chars gives 599,999.
    over = [bare] * 1000
    under = [*[bare] * 999, {**bare, "value": "x" * 553}]
    assert (_compact(under), _compact(over)) == (599_999, 600_001)
    assert ensure_ops_fit(under) == under
    with pytest.raises(PydanticCustomError) as exc:
        ensure_ops_fit(over)
    assert str(exc.value) == "ops hold 600001 characters of JSON; the limit is 600000"


def test_ops_are_capped_at_2000_before_parsing():
    from primer.canvas.ops import MAX_BATCH_STATEMENTS

    assert MAX_BATCH_STATEMENTS == 2000
    assert ensure_ops_fit(_clears(MAX_BATCH_STATEMENTS)) == _clears(MAX_BATCH_STATEMENTS)
    with pytest.raises(PydanticCustomError) as exc:
        ensure_ops_fit(_clears(MAX_BATCH_STATEMENTS + 1))
    assert exc.value.type == "ops_too_many"
    assert str(exc.value) == "a batch holds at most 2000 ops; got 2001"


def test_the_size_is_counted_in_characters_not_bytes_or_escapes():
    pad = "\u00e9" * 300_000
    ops = [{"op": "prop", "id": "a", "prop": "b", "value": pad}]
    assert len(json.dumps(ops, separators=(",", ":"), ensure_ascii=False)) < MAX_OPS_CHARS < len(json.dumps(ops, separators=(",", ":")))
    assert ensure_ops_fit(ops) is ops


def test_none_and_things_that_are_not_json():
    assert ensure_ops_fit(None) is None
    with pytest.raises(Exception) as exc:
        ensure_ops_fit([{"op": object()}])
    assert "ops must be a list of JSON objects" in str(exc.value)
    cyclic: list = []
    cyclic.append(cyclic)
    with pytest.raises(Exception) as exc:
        ensure_ops_fit(cyclic)
    assert "ops must be a list of JSON objects" in str(exc.value)


class _Batch(BaseModel):
    ops: list[CanvasOp] | None = None

    @field_validator("ops", mode="before")
    @classmethod
    def _fit(cls, raw):
        return ensure_ops_fit(raw)


def test_the_size_check_runs_before_any_op_is_built():
    huge = [{"op": "set", "id": "t", "type": "Text", "props": {"text": "x" * 10_000}}] * 70
    with pytest.raises(ValidationError) as exc:
        _Batch.model_validate({"ops": huge})
    [error] = exc.value.errors()
    assert error["type"] == "ops_too_large" and error["loc"] == ("ops",)
    assert error["msg"].startswith("ops hold ") and "the limit is 600000" in error["msg"]


# ---- the malformed ops, by number --------------------------------------------------------------------------------------------------------


def _problems(*ops: object):
    with pytest.raises(ValidationError) as exc:
        _Batch.model_validate({"ops": list(ops)})
    return ops_errors(exc.value)


def test_each_malformed_op_is_one_error_with_its_number_and_the_shapes():
    errors = _problems({"op": "clear"}, {"op": "set", "id": "t"}, {"op": "prop", "id": "a"}, {"op": "nope"})
    assert [(e.line, e.unit, e.message) for e in errors] == [
        (2, "op", "type: Field required"),
        (3, "op", "prop: Field required; value: Field required"),
        (4, "op", "op must be one of set, prop, append, delete or clear"),
    ]
    assert all(e.expected == " | ".join(OP_SHAPES) for e in errors)
    assert [e.render() for e in errors][0].startswith("op 2: type: Field required (")


def test_an_error_with_no_op_in_it_is_not_an_ops_error():
    class Both(_Batch):
        base_rev: int | None = None

    with pytest.raises(ValidationError) as exc:
        Both.model_validate({"ops": [{"op": "nope"}], "base_rev": "x"})
    assert ops_errors(exc.value) is None
    with pytest.raises(ValidationError) as exc:
        Both.model_validate({"ops": "not a list"})
    assert ops_errors(exc.value) is None, "the ops field itself is wrong: there is no op to number"


def test_an_op_with_many_problems_shows_three_and_counts_the_rest():
    [error] = _problems({"op": "set", "id": 1, "type": 2, "props": 3, "children": 4})
    assert error.message.count(";") == 3 and error.message.endswith("; and 1 more problems")


def test_names_the_input_chose_are_clipped():
    [error] = _problems({"op": "set", "id": "t", "type": "Text", "k" * 500: 1})
    assert len(error.message) < 100 and error.message.endswith("...: Extra inputs are not permitted")
