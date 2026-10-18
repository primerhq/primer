"""Properties that hold for any program: nothing crashes, a batch is whole, an accepted canvas is sound."""

from __future__ import annotations

import copy
import json
import random

from primer.canvas.catalog import CATALOG, param_for
from primer.canvas.lang import CanvasLangError, parse_program
from primer.canvas.state import CanvasState, Changed, Limits, apply_batch, json_equal
from tests.canvas._program_gen import UNPARSEABLE, program

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
