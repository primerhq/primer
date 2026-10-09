"""A pasted graph spec is refused for a field of the wrong type BEFORE the builder draws it (review of #646, round 2).

The import ran the would-be draft through the builder's pure functions, which catches a list that is an object but not a field that the INSPECTOR, the outline or the reference picker
draw later: an object where a string belongs is a React child that throws, a string where a list belongs is iterable and then has no ``.map``. The reviewer drove 35 shapes through the
real flow and 18 crashed a render path. ``GB_importProblem`` now walks the spec by the shape of the server's model (``primer/model/graph.py``): text fields are strings (or absent),
lists are lists, counts are numbers, a schema is an object whose properties are objects with a text ``type`` and ``description``. Every case below is a shape from that review; the controls
are shapes a real graph has (they must keep loading); ``tests/ui/test_graph_builder_import_renders.py`` draws what is accepted, in the real builder.
"""

from __future__ import annotations

import copy
import json

import pytest

from tests.ui.test_graph_builder_parity import _BEGIN, _END, _agent, _ctx, _edge, _import, _reduce

WRONG_TYPE = (
    "The spec has a field of the wrong type, so it was not loaded: `nodes`, `edges`, every router's `branches` and `conditions`, every fan-out's `specs` and every schema's `required` "
    "must be lists; ids, descriptions, templates and paths must be strings; `count` and `max_iterations` must be numbers; and a schema must be an object."
)

BASE = {
    "id": "g", "description": "base graph", "max_iterations": 3, "on_max_iterations": "e",
    "nodes": [
        {"kind": "begin", "id": "s", "description": "Start", "input_schema": {"type": "object", "properties": {"q": {"type": "string", "description": "the question"}}}},
        {"kind": "agent", "id": "a", "description": "Ask", "agent_id": "x", "input_template": "{{ initial_input }}",
         "response_format": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}},
        {"kind": "fan_out", "id": "f", "description": "Split", "specs": [{"kind": "broadcast", "target_node_id": "w", "count": 2, "on_failure": "collect"}]},
        {"kind": "agent", "id": "w", "description": "Work", "agent_id": "x", "input_template": "{{ nodes.a.text }}"},
        {"kind": "fan_in", "id": "m", "description": "Merge", "aggregate_template": ""},
        {"kind": "tool_call", "id": "t", "description": "Tool", "tool_id": "ts__echo", "arguments": {"msg": "{{ nodes.a.text }}"}},
        {"kind": "graph", "id": "sg", "description": "Sub", "graph_id": "other", "input_template": ""},
        {"kind": "end", "id": "e", "description": "Finish", "output_template": "{{ nodes.m.text }}", "output_schema": {"type": "object", "properties": {"r": {"type": "string"}}}},
    ],
    "edges": [
        {"kind": "static", "from_node": "s", "to_node": "a"},
        {"kind": "conditional", "from_node": "a", "router": {"kind": "json_path", "branches": [{"conditions": [{"path": "ok", "op": "eq", "value": True}], "to_node": "f"}], "default_to": "t"}},
        {"kind": "static", "from_node": "w", "to_node": "m"},
        {"kind": "static", "from_node": "m", "to_node": "e"},
        {"kind": "static", "from_node": "t", "to_node": "sg"},
        {"kind": "static", "from_node": "sg", "to_node": "e"},
    ],
}


def _node(spec: dict, node_id: str) -> dict:
    return next(n for n in spec["nodes"] if n["id"] == node_id)


def _set_node(node_id: str, key: str, value):
    return lambda s: _node(s, node_id).__setitem__(key, value)


def _set_edge(idx: int, key: str, value):
    return lambda s: s["edges"][idx].__setitem__(key, value)


def _set_router(idx: int, key: str, value):
    return lambda s: s["edges"][idx]["router"].__setitem__(key, value)


def _branches(value):
    return _set_router(1, "branches", value)


OBJ = {"x": 1}

# every shape of the review that crashed a render path, and the ones it accepted that no field of the model takes
REFUSED = {
    "max_iterations is an object": lambda s: s.__setitem__("max_iterations", OBJ),
    "on_max_iterations is an object": lambda s: s.__setitem__("on_max_iterations", OBJ),
    "a node kind is an object": _set_node("w", "kind", OBJ),
    "agent_id is an object": _set_node("a", "agent_id", OBJ),
    "agent_id is a list of objects": _set_node("a", "agent_id", [OBJ]),
    "agent_id is an object with a toString": _set_node("a", "agent_id", {"toString": 1}),
    "graph_id is an object": _set_node("sg", "graph_id", OBJ),
    "tool_id is an object": _set_node("t", "tool_id", OBJ),
    "input_template is an object": _set_node("w", "input_template", OBJ),
    "arguments is a string": _set_node("t", "arguments", "abc"),
    "response_format is a string": _set_node("a", "response_format", "abc"),
    "response_format required is an object": lambda s: _node(s, "a")["response_format"].__setitem__("required", {}),
    "response_format required is a number": lambda s: _node(s, "a")["response_format"].__setitem__("required", 5),
    "response_format required holds a non-string": lambda s: _node(s, "a")["response_format"].__setitem__("required", [OBJ]),
    "a response_format property type is an object": lambda s: _node(s, "a")["response_format"]["properties"]["ok"].__setitem__("type", OBJ),
    "an input_schema property description is an object": lambda s: _node(s, "s")["input_schema"]["properties"]["q"].__setitem__("description", OBJ),
    "an input_schema property is a string": lambda s: _node(s, "s")["input_schema"]["properties"].__setitem__("q", "abc"),
    "an input_schema properties is a list": lambda s: _node(s, "s")["input_schema"].__setitem__("properties", []),
    "output_schema required is true": lambda s: _node(s, "e")["output_schema"].__setitem__("required", True),
    "fan-out specs is a string": _set_node("f", "specs", "abc"),
    "a broadcast count is an object": _set_node("f", "specs", [{"kind": "broadcast", "target_node_id": "w", "count": {"n": 3}}]),
    "a broadcast count is a list of objects": _set_node("f", "specs", [{"kind": "broadcast", "target_node_id": "w", "count": [{"n": 3}]}]),
    "a map source_node_id is an object": _set_node("f", "specs", [{"kind": "map", "target_node_id": "w", "source_node_id": OBJ, "source_path": "items"}]),
    "a fan-out spec is a string": _set_node("f", "specs", ["abc"]),
    "a node x is an object": _set_node("w", "x", OBJ),
    "a node y is a string": _set_node("w", "y", "z"),
    "a static edge from_node is an object": _set_edge(2, "from_node", OBJ),
    "a static edge to_node is an object": _set_edge(2, "to_node", OBJ),
    "an edge kind is an object": _set_edge(0, "kind", OBJ),
    "a router is a string": _set_edge(1, "router", "abc"),
    "a branch to_node is an object": _branches([{"conditions": [], "to_node": OBJ}]),
    "conditions is a string": _branches([{"conditions": "abc", "to_node": "f"}]),
    "a condition is null": _branches([{"conditions": [None], "to_node": "f"}]),
    "a condition path is an object": _branches([{"conditions": [{"path": OBJ, "op": "eq", "value": 1}], "to_node": "f"}]),
    "a condition op is an object": _branches([{"conditions": [{"path": "ok", "op": OBJ, "value": 1}], "to_node": "f"}]),
    "a callable router callable_id is an object": _set_edge(1, "router", {"kind": "callable", "callable_id": OBJ}),
    "a router with no kind has conditions as an object": _set_edge(1, "router", {"branches": [{"conditions": OBJ, "to_node": "f"}], "default_to": "t"}),
    "a router with no kind has a null condition": _set_edge(1, "router", {"branches": [{"conditions": [None], "to_node": "f"}], "default_to": "t"}),
    "default_to is an object": _set_router(1, "default_to", OBJ),
}


def _mutated(mutate) -> dict:
    spec = copy.deepcopy(BASE)
    mutate(spec)
    return spec


@pytest.mark.parametrize("label", sorted(REFUSED))
def test_a_field_of_the_wrong_type_is_refused_with_one_message_and_the_draft_is_not_touched(label: str) -> None:
    result = _import(_mutated(REFUSED[label]))
    assert result["thrown"] == WRONG_TYPE and result["dispatched"] == [], label


def test_the_control_the_base_spec_loads() -> None:
    result = _import(BASE)
    assert result["thrown"] is None and len(result["dispatched"]) == 1


ACCEPTED = {
    "optional text fields are null": lambda s: _node(s, "a").update({"profile_id": None, "input_template": None, "description": None}),
    "a schema is null": lambda s: _node(s, "a").__setitem__("response_format", None),
    "a schema type is a list of strings": lambda s: _node(s, "s")["input_schema"]["properties"]["q"].__setitem__("type", ["string", "null"]),
    "a schema property has no type": lambda s: _node(s, "s")["input_schema"]["properties"].__setitem__("q", {"description": "free"}),
    "a nested object schema": lambda s: _node(s, "s")["input_schema"]["properties"].__setitem__("o", {"type": "object", "properties": {"k": {"type": "string"}}}),
    "an array schema with items": lambda s: _node(s, "s")["input_schema"]["properties"].__setitem__("l", {"type": "array", "items": {"type": "string"}}),
    "a property-level required flag (draft 3)": lambda s: _node(s, "s")["input_schema"]["properties"]["q"].__setitem__("required", True),
    "tool arguments hold any value": lambda s: _node(s, "t").__setitem__("arguments", {"a": {"b": [1, 2]}, "c": None}),
    "a condition value is any JSON": _branches([{"conditions": [{"path": "k", "op": "in", "value": [1, {"a": 2}]}], "to_node": "f"}]),
    "a router has no default": lambda s: s["edges"][1]["router"].pop("default_to"),
    "a tee spec lists its targets": _set_node("f", "specs", [{"kind": "tee", "target_node_ids": ["w", "t"]}]),
    "a callable router": _set_edge(1, "router", {"kind": "callable", "callable_id": "pkg.router"}),
    "unknown extra keys are carried": lambda s: (s.__setitem__("x-note", OBJ), _node(s, "a").__setitem__("x-extra", [OBJ])),
    "positions on nodes": _set_node("w", "x", 120.5),
    "no max_iterations": lambda s: (s.pop("max_iterations"), s.pop("on_max_iterations")),
}


@pytest.mark.parametrize("label", sorted(ACCEPTED))
def test_a_shape_a_real_graph_has_still_loads(label: str) -> None:
    """The controls: a walk that refused these would stop a real graph loading."""
    result = _import(_mutated(ACCEPTED[label]))
    assert result["thrown"] is None and len(result["dispatched"]) == 1, label


# ---------------------------------------------------------------------------
# an import replaces what a PUT replaces: a spec with no edges has none
# ---------------------------------------------------------------------------


def test_a_spec_with_no_edges_key_leaves_the_draft_no_edges() -> None:
    """A PUT of a body with no ``edges`` clears them. The reducer kept the OLD edges, so they pointed at nodes the spec had replaced."""
    current = {"id": "g", "description": "d", "nodes": [_BEGIN, _agent("a"), _END], "edges": [_edge("s", "a"), _edge("a", "e")]}
    out = _reduce(current, {"type": "IMPORT_SPEC", "spec": {"nodes": [_BEGIN]}})
    assert out["edges"] == []
    assert [n["id"] for n in out["nodes"]] == ["s"]


def test_a_spec_with_edges_replaces_them() -> None:
    current = {"id": "g", "description": "d", "nodes": [_BEGIN, _agent("a"), _END], "edges": [_edge("s", "a"), _edge("a", "e")]}
    out = _reduce(current, {"type": "IMPORT_SPEC", "spec": {"nodes": [_BEGIN, _END], "edges": [_edge("s", "e")]}})
    assert out["edges"] == [_edge("s", "e")]


def test_applying_a_starter_still_keeps_edges_it_does_not_name() -> None:
    """``APPLY_TEMPLATE`` is a merge and stays one."""
    current = {"id": "g", "description": "d", "nodes": [], "edges": [_edge("s", "e")]}
    out = _reduce(current, {"type": "APPLY_TEMPLATE", "spec": {"nodes": [_BEGIN]}})
    assert out["edges"] == [_edge("s", "e")]


# ---------------------------------------------------------------------------
# each step of the dry run is part of it: a mutant that drops one must be seen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step", ["GB_validate", "GB_stripAll", "GB_supersteps", "GB_allLinks"])
def test_each_step_of_the_dry_run_can_refuse_a_spec(step: str) -> None:
    """A spec that loads cleanly is refused when ONE of the pure functions the builder draws with throws, so dropping any of them from the dry run is seen."""
    ctx = _ctx()
    refusal = ctx.eval(f"(function () {{ {step} = function () {{ throw new TypeError('stubbed'); }}; return GB_importProblem({json.dumps(BASE)}); }})()")
    assert refusal == WRONG_TYPE, step


def test_the_dry_run_validates_with_the_same_options_the_builder_renders_with() -> None:
    """The builder validates with ``knownToolIds`` (the catalogue); a dry run with other options could miss what the real one throws on."""
    ctx = _ctx()
    seen = ctx.eval(
        "(function () { var seen = null; var real = GB_validate; GB_validate = function (d, o) { seen = o; return real(d, o); };"
        f" GB_applyImport({json.dumps(BASE)}, function () {{}}, {{ knownToolIds: ['ts__echo'] }}); return JSON.stringify(seen); }})()"
    )
    assert json.loads(seen) == {"knownToolIds": ["ts__echo"]}


# ---------------------------------------------------------------------------
# a spec too big for the stack is not "a field of the wrong type"
# ---------------------------------------------------------------------------

TOO_BIG = "The spec is too large or too deeply nested to load."


def test_a_stack_overflow_in_the_dry_run_has_its_own_message() -> None:
    ctx = _ctx()
    refusal = ctx.eval(f"(function () {{ GB_supersteps = function () {{ throw new RangeError('Maximum call stack size exceeded'); }}; return GB_importProblem({json.dumps(BASE)}); }})()")
    assert refusal == TOO_BIG


def test_a_schema_nested_far_deeper_than_any_graph_has_its_own_message() -> None:
    ctx = _ctx()
    refusal = ctx.eval(
        "(function () { var s = { type: 'string' }; for (var i = 0; i < 60000; i++) s = { type: 'object', properties: { k: s } };"
        " return GB_importProblem({ nodes: [{ kind: 'begin', id: 's', input_schema: s }], edges: [] }); })()"
    )
    assert refusal == TOO_BIG


def _chain_ctx(length: int):
    ctx = _ctx()
    ctx.eval(
        "var CHAIN = (function (n) { var nodes = [{ kind: 'begin', id: 'n0' }], edges = [];"
        " for (var i = 1; i < n; i++) { nodes.push({ kind: 'agent', id: 'n' + i, agent_id: 'x' }); edges.push({ kind: 'static', from_node: 'n' + (i - 1), to_node: 'n' + i }); }"
        f" return {{ nodes: nodes, edges: edges }}; }})({length});"
    )
    return ctx


def test_has_cycle_does_not_overflow_the_stack_on_a_long_chain() -> None:
    """``GB_hasCycle`` recursed once per step: a 30,000-step chain (a valid graph, however unlikely) overflowed the stack inside the validator, on every render."""
    ctx = _chain_ctx(30_000)
    assert ctx.eval("GB_hasCycle(CHAIN)") is False


def test_has_cycle_still_finds_a_cycle_at_the_end_of_a_long_chain() -> None:
    ctx = _chain_ctx(30_000)
    ctx.eval("CHAIN.edges.push({ kind: 'static', from_node: 'n29999', to_node: 'n5' });")
    assert ctx.eval("GB_hasCycle(CHAIN)") is True


def test_has_cycle_on_a_small_graph() -> None:
    ctx = _ctx()
    assert ctx.eval(f"GB_hasCycle({json.dumps({'nodes': [_BEGIN, _agent('a'), _END], 'edges': [_edge('s', 'a'), _edge('a', 'e')]})})") is False
    assert ctx.eval(f"GB_hasCycle({json.dumps({'nodes': [_BEGIN, _agent('a'), _END], 'edges': [_edge('s', 'a'), _edge('a', 's')]})})") is True
    assert ctx.eval(f"GB_hasCycle({json.dumps({'nodes': [_agent('a')], 'edges': [_edge('a', 'a')]})})") is True, "a self-loop is a cycle"
