"""JSON ops: the alternative input to Canvas Lang, and the normalized form records keep."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from primer.canvas.lang import Append, Assign, Clear, Delete, Ref, SetProp, parse_program
from primer.canvas.ops import (
    AppendOp,
    CanvasOp,
    ClearOp,
    DeleteOp,
    PropOp,
    SetOp,
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
