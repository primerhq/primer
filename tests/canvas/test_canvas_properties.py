"""Properties that hold for any program: nothing crashes, a batch is whole, an accepted canvas is sound and is
the one its own program rebuilds."""

from __future__ import annotations

import copy
import json
import random
from typing import Any

from primer.canvas.catalog import CATALOG, HTML_VALUE_MAX_CHARS, has_unstorable_char, param_for
from primer.canvas.decompile import decompile
from primer.canvas.lang import CanvasLangError, SetProp, format_value, parse_program
from primer.canvas.state import CanvasState, Changed, Limits, apply_batch, json_equal
from tests.canvas._program_gen import UNPARSEABLE, nested, program

def assert_sound(state: CanvasState) -> None:
    """The invariants every accepted canvas keeps."""
    elements = state.elements
    assert elements["root"]["type"] == "Column"
    parent_of: dict[str, str] = {}
    for eid, element in elements.items():
        spec = CATALOG[element["type"]]
        assert set(element) <= {"type", "props", "children"}
        assert ("children" in element) == spec.container
        for name, value in element["props"].items():
            param = param_for(spec, name)
            assert param is not None, (eid, name)
            assert param.default is None or not json_equal(value, param.default), f"{eid}.{name} stores its default"
        for child in element.get("children", []):
            assert child in elements, f"{eid} lists {child}, which does not exist"
            assert child not in parent_of, f"{child} has two parents"
            parent_of[child] = eid
    assert "root" not in parent_of
    seen: set[str] = set()
    stack = ["root"]
    while stack:
        eid = stack.pop()
        assert eid not in seen, "a cycle"
        seen.add(eid)
        stack.extend(elements[eid].get("children", []))
    assert seen == set(elements), f"unreachable: {set(elements) - seen}"
    assert set(state.value_set_by) == set(state.values) <= set(elements)
    for eid, element in elements.items():
        if element["type"] != "Select":
            continue
        props = element["props"]
        choices = [o if isinstance(o, str) else o[0] for o in props["options"]]
        for value in (props.get("value"), state.values.get(eid)):
            if value is None:
                continue
            assert all(v in choices for v in value) if props.get("multiple") else value in choices, (eid, value)
    assert state.size_bytes() <= Limits().max_canvas_bytes
    json.dumps(state.to_json())


def assert_changed_matches(before: CanvasState, after: CanvasState, changed: Changed) -> None:
    assert set(changed.added) == set(after.elements) - set(before.elements)
    assert set(changed.removed) == set(before.elements) - set(after.elements)
    expected = {
        eid
        for eid in after.elements
        if eid in before.elements
        and (after.elements[eid] != before.elements[eid] or not json_equal(before.current_value(eid), after.current_value(eid)))
    }
    assert set(changed.updated) == expected


def _users_choose(r: random.Random, state: CanvasState) -> None:
    """Stand in for the people at the keyboard: now and then a user picks an option."""
    for eid, element in state.elements.items():
        if element["type"] == "Select" and r.random() < 0.3:
            choices = [o if isinstance(o, str) else o[0] for o in element["props"]["options"]]
            state.values[eid] = [choices[0]] if element["props"].get("multiple") else choices[0]
            state.value_set_by[eid] = "user"


def test_a_generated_program_is_applied_or_refused_whole_and_accepted_canvases_are_sound():
    accepted = refused = changed = 0
    for seed in range(150):
        r = random.Random(seed)
        state = CanvasState.empty()
        for _ in range(6):
            text = program(r, state)
            try:
                statements = parse_program(text)
            except CanvasLangError:
                assert UNPARSEABLE in text, text
                continue
            frozen = copy.deepcopy(state)
            result = apply_batch(state, statements, limits=Limits(max_elements=15))
            assert state == frozen, "apply_batch mutated its input"
            if result.state is None:
                assert result.errors and result.changed == Changed() and result.warnings == ()
                refused += 1
                continue
            assert result.errors == ()
            assert_sound(result.state)
            assert_changed_matches(state, result.state, result.changed)
            assert result.state.rev == state.rev
            accepted += 1
            changed += result.changed != Changed()
            state = result.state
            _users_choose(r, state)
    # the generator must really reach the success paths, or this test proves nothing
    assert accepted > 500 and changed > 400 and refused > 100, (accepted, changed, refused)


# --- whatever apply_batch accepts survives the program canvas_get prints ------------------------------------------
#
# canvas_get returns "a program that recreates the canvas" (design 8.4). The language reads integers of at most 15
# digits and nests lists and objects at most 32 levels, but a JSON op never meets the parser: it becomes a statement
# directly. So the batch has to hold an Html value to the parser's limits itself, and these tests hold it to them.

_BASE = apply_batch(CanvasState.empty(), parse_program('root = Column([v])\nv = Html("<b>x</b>")'), limits=Limits()).state


def _current_values(state: CanvasState) -> dict[str, Any]:
    return {eid: state.current_value(eid) for eid in state.elements if state.current_value(eid) is not None}


def assert_survives_its_program(state: CanvasState) -> None:
    """apply -> decompile -> parse -> apply: the program rebuilds this canvas, and sent back as it is changes nothing."""
    rebuilt = apply_batch(CanvasState.empty(), parse_program(decompile(state, full=True)), limits=Limits())
    assert rebuilt.errors == (), [e.render() for e in rebuilt.errors]
    assert rebuilt.state.elements == state.elements
    expected, got = _current_values(state), _current_values(rebuilt.state)
    assert got.keys() == expected.keys() and all(json_equal(got[k], expected[k]) for k in got)
    again = apply_batch(state, parse_program(decompile(state)), limits=Limits())
    assert again.errors == () and again.changed == Changed() and again.state == state


def _set_value(value: Any):
    return apply_batch(_BASE, [SetProp(1, "v", "value", value)], limits=Limits())


# (what it is, the value, whether the batch must accept it)
VALUE_POOL = [
    ("true", True, True), ("false", False, True), ("zero", 0, True), ("minus one", -1, True),
    ("10**15 - 1", 10**15 - 1, True), ("-(10**15 - 1)", -(10**15 - 1), True),
    ("10**15", 10**15, False), ("-(10**15)", -(10**15), False), ("10**16", 10**16, False), ("10**30", 10**30, False),
    ("10**308", 10**308, False), ("10**309", 10**309, False), ("10**400", 10**400, False),
    ("10**5000", 10**5000, False), ("-(10**5000)", -(10**5000), False),
    ("0.0", 0.0, True), ("-0.0", -0.0, True), ("1.5", 1.5, True), ("1e-7", 1e-7, True), ("1e16", 1e16, True),
    ("1e300", 1e300, True), ("5e-324", 5e-324, True), ("the largest float", 1.7976931348623157e308, True),
    ("nan", float("nan"), False), ("inf", float("inf"), False), ("-inf", -float("inf"), False),
    ("empty text", "", True), ("text", "x", True), ("unicode", "caf\u00e9 \U0001F600", True), ("quotes", 'say "hi"', True),
    ("backslash", "back\\slash", True), ("controls", "a\nb\tc\r", True), ("hash", "# not a comment", True),
    ("apostrophe", "it's", True), ("triple quote", '"""', True), ("four quotes", '""""', True),
    ("separators", "\u2028 \u2029 \x85 \ufeff", True), ("keep", "...", True),
    ("NUL", "a\x00b", False), ("lone surrogate", "\ud800", False),
    ("empty list", [], True), ("empty object", {}, True), ("lists", [1, [2, [3]]], True),
    ("mixed", {"k": [1, {"j": None}]}, True), ("empty key", {"": ""}, True), ("quoted key", {'a"b': "c"}, True),
    ("NUL in a key", {"a\x00": 1}, False), ("int key", {1: 2}, False),
    ("list of 10**15 - 1", [10**15 - 1], True), ("list of 10**15", [10**15], False),
    ("object of 10**16", {"k": 10**16}, False), ("list of nan", [float("nan")], False),
    ("1 deep", nested(1), True), ("31 deep", nested(31), True), ("32 deep", nested(32), True),
    ("33 deep", nested(33), False), ("34 deep", nested(34), False), ("200 deep", nested(200), False),
    ("32 deep objects", nested(32, "o"), True), ("33 deep objects", nested(33, "o"), False),
    ("32 deep mixed", nested(32, "lo"), True), ("33 deep mixed", nested(33, "lo"), False), ("33 deep mixed 2", nested(33, "ol"), False),
    ("998 characters", "x" * 998, True), ("999 characters", "x" * 999, False),
]


def test_the_value_pool_is_accepted_or_refused_where_the_parser_would_read_it_back():
    for label, value, accepted in VALUE_POOL:
        result = _set_value(value)
        assert (result.state is not None) is accepted, (label, [e.render() for e in result.errors])
        if accepted:
            assert_survives_its_program(result.state)


_SCALARS = [
    True, False, 0, 1, -1, 10**14, 10**15 - 1, -(10**15 - 1), 10**15, -(10**15), 10**16, 10**30, 10**308, 10**309, 10**400,
    0.0, -0.0, 0.5, -2.25, 1e-7, 1e16, 1e22, 1e300, 5e-324, float("nan"), float("inf"),
    "", "x", "caf\u00e9 \U0001F600", 'say "hi"', "back\\slash", "a\nb", "# hash", "it's", '"""', "\u2028", "\x00", "\ud800",
    "x" * 990, "x" * 998, "x" * 999,
]


def _random_value(r: random.Random) -> Any:
    value: Any = r.choice(_SCALARS) if r.random() < 0.7 else r.choice([[], {}, [r.choice(_SCALARS), r.choice(_SCALARS)], {"k": r.choice(_SCALARS)}])
    for _ in range(r.choice([0, 0, 1, 2, 5, 10, 30, 31, 32, 33, 34, 40])):
        shape = r.random()
        value = [value] if shape < 0.5 else {"k": value} if shape < 0.8 else [r.choice(_SCALARS), value]
    return value


def _the_parser_reads_it_back(value: Any) -> bool:
    """What the batch must say about an Html value, worked out from the parser itself and not from the batch's own rules."""
    try:
        [statement] = parse_program(f"a.x = {format_value(value)}")
    except (ValueError, CanvasLangError):
        return False
    return (
        json_equal(statement.value, value)
        and not has_unstorable_char(value)
        and len(json.dumps(value, separators=(",", ":"), ensure_ascii=False)) <= HTML_VALUE_MAX_CHARS
    )


def test_a_random_html_value_is_accepted_exactly_when_the_parser_reads_it_back_and_then_survives_its_program():
    accepted = refused = 0
    for seed in range(600):
        value = _random_value(random.Random(seed))
        result = _set_value(value)
        assert (result.state is not None) is _the_parser_reads_it_back(value), (seed, [e.render() for e in result.errors])
        if result.state is None:
            refused += 1
            continue
        accepted += 1
        assert_survives_its_program(result.state)
    assert accepted > 150 and refused > 150, (accepted, refused)       # the draw must reach both sides, or this proves nothing
