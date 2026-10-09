"""What the builder ACCEPTS from a pasted spec, it can draw; and what it cannot draw does not take the console down (review of #646, round 2).

The reviewer drove the real flow (JSON, ``GR_ImportSpecModal.onApply``, re-render) through the server's Babel and found 18 of the 35 shapes the import accepted crashing a render path. The static
table in ``test_graph_builder_import_shapes.py`` says which shapes are refused. This file asks the question the other way round, in the real builder files on the strict mini React
(``tests/ui/_mini_react.py``: an object child throws, as in React):

* a property test: take a graph that draws, put a wrong-typed value at EVERY position of it, and for each result either the import refuses it or the builder, the canvas label, every node's
  inspector, every reference picker and every edge's inspector draw it. A position the walk of ``GB_importProblem`` misses shows up here by name;
* the builder has an error boundary: a render that throws below it shows a message with Undo and Discard instead of unmounting the root, and the draft (which lives above the boundary) is not lost;
* a Load clears the selection (a selected step the spec replaced would be drawn from a draft that no longer has it).
"""

from __future__ import annotations

import copy
import functools
import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context
from tests.ui.test_graph_builder_import_shapes import BASE

# The second graph the property test mutates: the other shapes of fan-out spec (tee, map), a callable router and a router with no kind, which BASE does not have.
BASE_B = {
    "id": "g", "description": "second graph", "max_iterations": 2,
    "nodes": [
        {"kind": "begin", "id": "s", "input_schema": {"type": "object", "properties": {"items": {"type": ["array", "null"], "description": "work list"}}, "required": []}},
        {"kind": "fan_out", "id": "f", "specs": [
            {"kind": "tee", "target_node_ids": ["w1", "w2"], "on_failure": "drain_then_fail"},
            {"kind": "map", "target_node_id": "w1", "source_node_id": "s", "source_path": "items", "on_failure": "fail_fast"},
        ]},
        {"kind": "agent", "id": "w1", "description": "One", "agent_id": "x", "profile_id": "p", "input_template": "{{ x }}", "response_format": {"type": "object", "properties": {"k": {"type": "string"}}}},
        {"kind": "agent", "id": "w2", "agent_id": "x"},
        {"kind": "fan_in", "id": "m", "aggregate_template": "{{ results }}", "output_schema": {"type": "object", "properties": {"n": {"type": "number", "description": "count"}}, "required": ["n"]}},
        {"kind": "tool_call", "id": "t", "tool_id": "ts__echo", "arguments_template": "{{ x }}", "output_schema": {"type": "object"}},
        {"kind": "end", "id": "e", "output_template": "{{ nodes.m.text }}"},
    ],
    "edges": [
        {"kind": "static", "from_node": "s", "to_node": "f"},
        {"kind": "static", "from_node": "w1", "to_node": "m"},
        {"kind": "static", "from_node": "w2", "to_node": "m"},
        {"kind": "conditional", "from_node": "m", "router": {"kind": "callable", "callable_id": "pkg.router"}},
        {"kind": "conditional", "from_node": "t", "router": {"branches": [{"conditions": [{"path": "k", "op": "contains", "value": "v"}], "to_node": "e"}], "default_to": "e"}},
        {"from_node": "t", "to_node": "e"},
    ],
}

from tests.ui._graph_builder_v8 import DRIVER as _DRIVER
from tests.ui._graph_builder_v8 import PRELUDE as _PRELUDE
from tests.ui._graph_builder_v8 import builder_code as _code

@pytest.fixture
def ctx():
    c = mini_react_context(_code(), _PRELUDE)
    c.eval(_DRIVER)
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# the property: accepted means drawn
# ---------------------------------------------------------------------------

WRONG_VALUES = [{"x": 1}, "abc", [{"x": 1}], 5, None, True, []]


def _positions(value, path=()):
    """Every position of a JSON value, as the path of keys/indexes to it (the root included)."""
    yield path
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _positions(child, (*path, key))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from _positions(child, (*path, i))


def _put(spec: dict, path: tuple, value) -> dict:
    out = copy.deepcopy(spec)
    cur = out
    for key in path[:-1]:
        cur = cur[key]
    cur[path[-1]] = value
    return out


def _mutations(base: dict = BASE) -> list[dict]:
    out = []
    for path in _positions(base):
        if path in ((), ("nodes",), ("edges",)):
            continue  # the spec itself, and the two lists the shape check before the walk already owns
        here = base
        for key in path:
            here = here[key]
        for wrong in WRONG_VALUES:
            if type(wrong) is type(here):
                continue
            out.append({"where": " / ".join(str(k) for k in path) + " = " + json.dumps(wrong), "spec": _put(base, path, wrong)})
    return out


@pytest.mark.parametrize("base", [BASE, BASE_B], ids=["base", "base_b"])
def test_the_property_test_really_has_something_to_try(base: dict) -> None:
    mutations = _mutations(base)
    assert len(mutations) > 250, len(mutations)
    assert len({m["where"] for m in mutations}) == len(mutations)


@pytest.mark.parametrize("base", [BASE, BASE_B], ids=["base", "base_b"])
def test_the_base_graph_is_drawn_by_every_part_of_the_builder(ctx, base: dict) -> None:
    """The control: the harness mounts the real builder, every inspector and every picker without a throw, so a crash below means the SPEC."""
    r = json.loads(ctx.eval(f"JSON.stringify(probeDraft({json.dumps(base)}, {json.dumps(base)}))"))
    assert r["refused"] is None and r["crashes"] == {}, r


@pytest.mark.parametrize("base", [BASE, BASE_B], ids=["base", "base_b"])
def test_a_wrong_typed_value_at_any_position_is_refused_or_drawn(ctx, base: dict) -> None:
    bad = json.loads(ctx.eval(f"JSON.stringify(probeAll({json.dumps(base)}, {json.dumps(_mutations(base))}))"))
    assert bad == [], f"{len(bad)} accepted shape(s) crash a render path, first: {json.dumps(bad[:3], indent=1)}"


# ---------------------------------------------------------------------------
# the error boundary
# ---------------------------------------------------------------------------


def _poisoned(**changes) -> dict:
    return {**copy.deepcopy(BASE), "description": "POISON", **changes}


def _has(ctx, testid: str) -> bool:
    return ctx.eval(f"MR.find({json.dumps(testid)}) !== null")


def _text(ctx) -> str:
    return " ".join(json.loads(ctx.eval("JSON.stringify(MR.texts())")))


def test_a_render_that_throws_below_the_builder_shows_a_message_instead_of_unmounting(ctx) -> None:
    ctx.eval(f"mountBuilder({json.dumps(BASE)});")
    assert _has(ctx, "gb-builder") and not _has(ctx, "gb-render-error")
    ctx.eval(f"load({json.dumps(_poisoned())});")
    assert _has(ctx, "gb-render-error"), "the throw reached the root: there is no boundary"
    assert "poison in the inspector" in _text(ctx)
    assert "could not draw" in _text(ctx).lower()


def test_undo_in_the_message_brings_back_the_draft_before_the_bad_load(ctx) -> None:
    ctx.eval(f"mountBuilder({json.dumps(BASE)});")
    ctx.eval(f"load({json.dumps(_poisoned())});")
    assert _has(ctx, "gb-render-error")
    ctx.eval('MR.click("gb-render-error-undo"); MR.rerender();')
    assert not _has(ctx, "gb-render-error") and _has(ctx, "gb-builder")
    assert ctx.eval("inspector().props.draft.description") == "base graph"
    assert ctx.eval("inspector().props.draft.nodes.length") == len(BASE["nodes"])


def test_discard_in_the_message_goes_back_to_the_saved_graph(ctx) -> None:
    ctx.eval(f"mountBuilder({json.dumps(BASE)});")
    ctx.eval(f"load({json.dumps(_poisoned(description='POISON'))});")
    assert _has(ctx, "gb-render-error")
    ctx.eval('MR.click("gb-render-error-discard"); MR.rerender();')
    assert not _has(ctx, "gb-render-error") and ctx.eval("inspector().props.draft.description") == "base graph"


def test_with_nothing_to_undo_the_undo_button_is_off(ctx) -> None:
    """A graph that fails the moment it is drawn has no history to go back to; Discard (back to the saved state) is the way out."""
    ctx.eval(f"mountBuilder({json.dumps(_poisoned())});")
    assert _has(ctx, "gb-render-error")
    assert ctx.eval('MR.find("gb-render-error-undo").props.disabled') is True


def test_the_boundary_catches_again_after_it_has_been_reset(ctx) -> None:
    ctx.eval(f"mountBuilder({json.dumps(BASE)});")
    ctx.eval(f"load({json.dumps(_poisoned())});")
    ctx.eval('MR.click("gb-render-error-undo"); MR.rerender();')
    assert not _has(ctx, "gb-render-error")
    ctx.eval(f"load({json.dumps(_poisoned())});")
    assert _has(ctx, "gb-render-error"), "a boundary that stays reset once would let the second throw through"


def test_the_message_stays_while_the_draft_is_unchanged(ctx) -> None:
    """A boundary that retries on every render would loop on a draft that still throws."""
    ctx.eval(f"mountBuilder({json.dumps(BASE)});")
    ctx.eval(f"load({json.dumps(_poisoned())});")
    ctx.eval("MR.rerender(); MR.rerender();")
    assert _has(ctx, "gb-render-error")


# ---------------------------------------------------------------------------
# a Load clears the selection
# ---------------------------------------------------------------------------


def test_loading_a_spec_clears_the_selected_step(ctx) -> None:
    ctx.eval(f"mountBuilder({json.dumps(BASE)}); selectNode('a');")
    assert ctx.eval("inspector().props.node && inspector().props.node.id") == "a"
    # a spec that has a step of the same id: the selection would otherwise land on the NEW step `a` and draw it from data the operator never looked at
    other = {"description": "other", "nodes": [{"kind": "begin", "id": "z"}, {"kind": "agent", "id": "a", "agent_id": "x"}], "edges": [{"kind": "static", "from_node": "z", "to_node": "a"}]}
    ctx.eval(f"load({json.dumps(other)});")
    assert ctx.eval("inspector().props.node") is None and ctx.eval("inspector().props.edgeIdx") is None


def test_loading_a_spec_clears_the_selected_edge(ctx) -> None:
    ctx.eval(f"mountBuilder({json.dumps(BASE)});")
    # select an edge through the canvas the way a click does
    ctx.eval("findType(MR.find('gb-builder'), GB_Canvas).props.onEdgeClick(0); MR.rerender();")
    assert ctx.eval("inspector().props.edgeIdx") == 0
    ctx.eval(f"load({json.dumps({'nodes': [{'kind': 'begin', 'id': 'z'}], 'edges': []})});")
    assert ctx.eval("inspector().props.edgeIdx") is None and ctx.eval("inspector().props.node") is None


# ---------------------------------------------------------------------------
# a boolean subschema is valid JSON Schema and is drawn
# ---------------------------------------------------------------------------


def test_a_boolean_subschema_is_accepted_and_drawn_by_every_part_of_the_builder(ctx) -> None:
    spec = copy.deepcopy(BASE)
    spec["nodes"][0]["input_schema"]["properties"].update({"ok": True, "no": False, "o": {"type": "object", "properties": {"deep": True}}})
    r = json.loads(ctx.eval(f"JSON.stringify(probeDraft({json.dumps(BASE)}, {json.dumps(spec)}))"))
    assert r["refused"] is None and r["crashes"] == {}, r


# ---------------------------------------------------------------------------
# a throw at click time: the message offers to let go of the selection
# ---------------------------------------------------------------------------


def test_a_step_that_throws_when_it_is_selected_offers_to_clear_the_selection(ctx) -> None:
    ctx.eval(f"__POISON_NODE = 'a'; mountBuilder({json.dumps(BASE)}); selectNode('a');")
    assert _has(ctx, "gb-render-error"), "the throw at selection reached the root"
    clear = ctx.eval('MR.find("gb-render-error-clear") !== null')
    assert clear, "the message offers Undo and Discard only, and both throw the import away"
    ctx.eval('MR.click("gb-render-error-clear"); MR.rerender();')
    assert not _has(ctx, "gb-render-error") and _has(ctx, "gb-builder")
    assert ctx.eval("inspector().props.node") is None and ctx.eval("inspector().props.edgeIdx") is None
    assert ctx.eval("inspector().props.draft.nodes.length") == len(BASE["nodes"]), "the draft is as it was"


def test_clearing_the_selection_leaves_the_undo_history_alone(ctx) -> None:
    ctx.eval(f"mountBuilder({json.dumps(BASE)});")
    ctx.eval(f"load({json.dumps({**copy.deepcopy(BASE), 'description': 'imported'})});")   # something to undo
    ctx.eval("__POISON_NODE = 'a'; selectNode('a');")
    assert _has(ctx, "gb-render-error") and ctx.eval('MR.find("gb-render-error-undo").props.disabled') is False
    ctx.eval('MR.click("gb-render-error-clear"); MR.rerender();')
    assert ctx.eval("inspector().props.draft.description") == "imported"
    ctx.eval("__POISON_NODE = null;")
    ctx.eval('MR.click("gb-json-tab");')   # the builder is alive: its own controls still work


def test_a_draft_that_throws_with_nothing_selected_has_no_clear_the_selection_button(ctx) -> None:
    """Only a selection can be cleared: the import clears it, so the POISON description message is Undo and Discard alone."""
    ctx.eval(f"mountBuilder({json.dumps(BASE)});")
    ctx.eval(f"load({json.dumps(_poisoned())});")
    assert _has(ctx, "gb-render-error")
    assert ctx.eval('MR.find("gb-render-error-clear")') is None
