"""What the retired legacy graph editor pinned, pinned on the live builder (GB_Builder) instead.

The older editor (``GR_GraphEditor`` in ``graphs.jsx``) was deleted. Its tests were source greps ("this word appears in graphs.jsx"); each property they named is checked here against the
builder, and where the backend model defines the vocabulary (node kinds, branch operators, fan-out kinds and failure modes, edge kinds) the builder is compared with the MODEL, so a kind
the backend learns and the builder does not know fails here.

The validation rules are executed for real in MiniRacer, one case per rule the old editor's ``GR_localViolations`` had.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import get_args

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
GB = UI / "components" / "graph-builder"


def _src(name: str) -> str:
    return (GB / name).read_text(encoding="utf-8")


def _literal(model, field: str) -> set[str]:
    return set(get_args(model.model_fields[field].annotation))


def _model_node_kinds() -> set[str]:
    from primer.model.graph import GraphNode

    members = get_args(get_args(GraphNode)[0])
    return {next(iter(get_args(m.model_fields["kind"].annotation))) for m in members}


# ---------------------------------------------------------------------------
# node kinds (was test_graphs_begin_end_nodes)
# ---------------------------------------------------------------------------


def test_the_builder_has_an_inspector_section_for_every_kind_the_model_has() -> None:
    meta = re.search(r"const GB_KIND_META = \{(.*?)\n\};", _src("gb-outline.jsx"), re.S)
    assert meta, "GB_KIND_META moved: update this pin"
    known = set(re.findall(r"^\s+(\w+): \{ tint", meta.group(1), re.M))
    kinds = _model_node_kinds()
    assert kinds == {"begin", "end", "agent", "graph", "tool_call", "fan_out", "fan_in"}, "the model grew a kind: give the builder a section, then update this pin"
    assert known == kinds, f"the builder's kinds differ from the model's: {known ^ kinds}"
    inspector = _src("gb-inspector.jsx")
    for kind in kinds:
        assert f'node.kind === "{kind}"' in inspector, f"the inspector has no section for {kind}"


def test_no_terminal_kind_anywhere_in_the_builder() -> None:
    for path in sorted(GB.glob("*.jsx")):
        assert '"terminal"' not in path.read_text(encoding="utf-8"), path.name


def test_end_comes_from_the_palette_and_begin_from_the_starters_or_the_readiness_fix() -> None:
    assert 'id: "end"' in _src("gb-palette.jsx")
    builder = _src("graph-builder.jsx")
    assert 'row.fix === "add_begin"' in builder
    assert 'GB_makeNode({ kind: "begin"' in builder


# ---------------------------------------------------------------------------
# per-kind fields (was test_graphs_side_panel_fields)
# ---------------------------------------------------------------------------

_FIELDS = {
    "agent": ("agent_id", "input_template", "response_format"),
    "tool_call": ("tool_id", "arguments", "arguments_template"),
    "graph": ("graph_id", "input_template"),
    "begin": ("input_schema",),
    "end": ("output_template", "output_schema"),
    "fan_in": ("aggregate_template",),
}


@pytest.mark.parametrize("kind", sorted(_FIELDS))
def test_the_inspector_writes_each_field_of_the_kind(kind: str) -> None:
    inspector = _src("gb-inspector.jsx")
    for field in _FIELDS[kind]:
        assert re.search(rf"patch\(\{{ {field}:", inspector), f"nothing in the inspector writes {kind}.{field}"


def test_every_node_has_a_description_title_and_a_step_id_rename() -> None:
    inspector = _src("gb-inspector.jsx")
    assert "patch({ description: e.target.value })" in inspector
    assert 'type: "RENAME_NODE"' in inspector


def test_the_save_body_carries_the_graph_description_and_max_iterations() -> None:
    body = re.search(r"const buildSaveBody = \(\) => \(\{(.*?)\n  \}\);", _src("graph-builder.jsx"), re.S)
    assert body, "buildSaveBody moved: update this pin"
    assert "description: draft.description" in body.group(1)
    assert "max_iterations: draft.max_iterations" in body.group(1)


# ---------------------------------------------------------------------------
# edges (was test_graphs_edge_modes, test_graphs_edge_selection, test_graphs_branch_editor)
# ---------------------------------------------------------------------------


def test_the_builder_draws_both_edge_kinds_the_model_has_and_static_is_the_default() -> None:
    from primer.model.graph import GraphEdge

    kinds = {next(iter(get_args(m.model_fields["kind"].annotation))) for m in get_args(get_args(GraphEdge)[0])}
    assert kinds == {"static", "conditional"}, "the model grew an edge kind: update the builder, then this pin"
    builder = _src("graph-builder.jsx")
    assert 'edge: { kind: "static", from_node: from, to_node: to }' in builder, "a drawn connection is static"
    inspector = _src("gb-inspector.jsx")
    assert re.search(r'kind: "conditional", to_node: undefined, router: \{ kind: "json_path"', inspector), "a static edge becomes a json_path choice"


def test_an_edge_can_be_selected_and_the_inspector_shows_it() -> None:
    builder = _src("graph-builder.jsx")
    assert "const [selectedEdge, setSelectedEdge] = useState(null)" in builder
    assert "onEdgeClick" in builder and "edgeIdx={selectedEdge}" in builder


def test_the_branch_editor_offers_exactly_the_operators_the_model_has() -> None:
    from primer.model.graph import BranchCondition

    ops = re.search(r"const GB_OPS = \[(.*?)\];", _src("gb-branches.jsx"), re.S)
    assert ops, "GB_OPS moved: update this pin"
    assert set(re.findall(r'"(\w+)"', ops.group(1))) == _literal(BranchCondition, "op")


def test_the_branch_editor_has_a_catch_all_and_a_callable_router_is_shown_read_only() -> None:
    branches = _src("gb-branches.jsx")
    assert "default_to" in branches and "In any other case" in branches
    assert 'router.kind === "callable"' in branches


@pytest.mark.parametrize(
    ("text", "op", "expected"),
    [
        ("5", "eq", 5),
        ("true", "eq", True),
        ("abc", "eq", "abc"),
        ('"abc"', "ne", "abc"),
        ("a, b ,c", "in", ["a", "b", "c"]),
        ("[1, 2]", "in", [1, 2]),
        ("5", "not_in", [5]),
        ("", "exists", ""),
    ],
)
def test_branch_values_are_parsed_the_way_the_router_compares_them(text: str, op: str, expected: object) -> None:
    """``GR_parseBranchValue`` stayed in ``graphs.jsx`` for the builder's branch editor (``gb-branches.jsx`` calls it); the old tests only grepped for operator names."""
    from py_mini_racer import MiniRacer

    graphs = (UI / "components" / "graphs.jsx").read_text(encoding="utf-8")
    fn = "function GR_parseBranchValue(" + graphs.split("function GR_parseBranchValue(", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    ctx = MiniRacer()
    ctx.eval(fn)
    assert json.loads(ctx.eval(f"JSON.stringify(GR_parseBranchValue({json.dumps(text)}, {json.dumps(op)}))")) == expected


# ---------------------------------------------------------------------------
# fan-out (was GR_FanOutSpecsEditor, which had no test of its own)
# ---------------------------------------------------------------------------


def test_the_fan_out_inspector_offers_every_spec_kind_and_failure_mode_the_model_has() -> None:
    from primer.model.graph import FanOutSpec

    body = _src("gb-inspector.jsx").split("function GB_FanOutBody", 1)[1]
    kinds = re.search(r"const KINDS = \[(.*?)\n  \];", body, re.S)
    fails = re.search(r"const FAILS = \[(.*?)\n  \];", body, re.S)
    assert kinds and fails, "the fan-out option lists moved: update this pin"
    assert set(re.findall(r'\{ id: "(\w+)"', kinds.group(1))) == _literal(FanOutSpec, "kind")
    assert set(re.findall(r'\{ id: "(\w+)"', fails.group(1))) == _literal(FanOutSpec, "on_failure")


# ---------------------------------------------------------------------------
# canvas (was test_editor_g6)
# ---------------------------------------------------------------------------


def test_the_builder_canvas_is_the_shared_g6_canvas_with_move_and_connect_wired() -> None:
    assert "<GR_Canvas" in _src("gb-canvas.jsx")
    builder = _src("graph-builder.jsx")
    assert 'type: "MOVE_NODE"' in builder and "onConnect=" in builder
    assert "layoutNonce={layoutNonce}" in builder, "a Tidy up bumps the nonce that re-seeds the canvas"


def test_the_builder_prints_no_raw_api_line() -> None:
    for path in sorted(GB.glob("*.jsx")):
        assert "GET /v1/graphs" not in path.read_text(encoding="utf-8"), path.name


# ---------------------------------------------------------------------------
# violations (was GR_localViolations / GR_ViolationsBanner): one case per rule it had
# ---------------------------------------------------------------------------


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis; window.primerApi = {};")
    for name in ("gb-model.jsx", "gb-refs.jsx", "gb-validate.jsx"):
        ctx.eval(_src(name))
    return ctx


def _validate(draft: dict, opts: dict | None = None) -> dict:
    return json.loads(_ctx().eval(f"JSON.stringify(GB_validate({json.dumps(draft)}, {json.dumps(opts or {})}))"))


def _agent(node_id: str) -> dict:
    return {"kind": "agent", "id": node_id, "agent_id": "x", "description": node_id}


def _edge(a: str, b: str) -> dict:
    return {"kind": "static", "from_node": a, "to_node": b}


_BEGIN = {"kind": "begin", "id": "s"}
_END = {"kind": "end", "id": "e", "output_template": ""}

_RULES = {
    "duplicate ids": ({"nodes": [_BEGIN, _agent("a"), _agent("a"), _END], "edges": [_edge("s", "a"), _edge("a", "e")]}, None, "blocking", "duplicate_id"),
    "no Begin": ({"nodes": [_agent("a"), _END], "edges": [_edge("a", "e")]}, None, "runnable", "begin_count"),
    "two Begins": ({"nodes": [_BEGIN, {"kind": "begin", "id": "s2"}, _END], "edges": [_edge("s", "e"), _edge("s2", "e")]}, None, "runnable", "begin_count"),
    "no End": ({"nodes": [_BEGIN, _agent("a")], "edges": [_edge("s", "a")]}, None, "runnable", "no_end"),
    "edge from an unknown node": ({"nodes": [_BEGIN, _agent("a"), _END], "edges": [_edge("s", "a"), _edge("a", "e"), _edge("ghost", "a")]}, None, "blocking", "unknown_from"),
    "edge to an unknown node": ({"nodes": [_BEGIN, _agent("a"), _END], "edges": [_edge("s", "a"), _edge("a", "e"), _edge("a", "ghost")]}, None, "blocking", "unknown_target"),
    "branch to an unknown node": (
        {"nodes": [_BEGIN, _agent("a"), _END], "edges": [_edge("s", "a"), {"kind": "conditional", "from_node": "a", "router": {
            "kind": "json_path", "branches": [{"conditions": [{"path": "k", "op": "eq", "value": 1}], "to_node": "ghost"}], "default_to": "e"}}]},
        None, "blocking", "unknown_target"),
    "End not reachable from Begin": ({"nodes": [_BEGIN, _agent("a"), _END], "edges": [_edge("s", "a")]}, None, "runnable", "end_unreachable"),
    "fan-out with an outgoing edge": (
        {"nodes": [_BEGIN, {"kind": "fan_out", "id": "f", "specs": []}, _agent("a"), _END], "edges": [_edge("s", "f"), _edge("f", "a"), _edge("a", "e")]}, None, "blocking", "fanout_has_edge"),
    "fan-out spec with an unknown target": (
        {"nodes": [_BEGIN, {"kind": "fan_out", "id": "f", "specs": [{"kind": "broadcast", "target_node_id": "ghost", "count": 2}]}, _END], "edges": [_edge("s", "f")]},
        None, "blocking", "fanout_unknown_target"),
    "fan-in with no incoming edge": ({"nodes": [_BEGIN, {"kind": "fan_in", "id": "m", "aggregate_template": ""}, _END], "edges": [_edge("s", "e")]}, None, "blocking", "fanin_no_incoming"),
    "conditional edge with no branches": (
        {"nodes": [_BEGIN, _agent("a"), _END], "edges": [_edge("s", "a"), {"kind": "conditional", "from_node": "a", "router": {"kind": "json_path", "branches": [], "default_to": "e"}}]},
        None, "blocking", "branches_none"),
    "router default_to to an unknown node": (
        {"nodes": [_BEGIN, _agent("a"), _END], "edges": [_edge("s", "a"), {"kind": "conditional", "from_node": "a", "router": {
            "kind": "json_path", "branches": [{"conditions": [{"path": "k", "op": "eq", "value": 1}], "to_node": "e"}], "default_to": "ghost"}}]},
        None, "blocking", "unknown_target"),
    "tool not in the catalogue": (
        {"nodes": [_BEGIN, {"kind": "tool_call", "id": "t", "tool_id": "nope", "arguments": {}}, _END], "edges": [_edge("s", "t"), _edge("t", "e")]},
        {"knownToolIds": ["toolset__real"]}, "warnings", "tool_unknown"),
}


def test_a_complete_draft_raises_none_of_them() -> None:
    """The control for the cases below: a rule that fired on every draft would pass them all."""
    clean = {"nodes": [_BEGIN, _agent("a"), _END], "edges": [_edge("s", "a"), _edge("a", "e")]}
    assert _validate(clean) == {"blocking": [], "runnable": [], "warnings": []}


@pytest.mark.parametrize("rule", sorted(_RULES))
def test_the_builder_validator_has_the_rule(rule: str) -> None:
    draft, opts, bucket, code = _RULES[rule]
    found = _validate(draft, opts)
    assert code in [row["code"] for row in found[bucket]], f"{rule}: expected {code} in {bucket}, got {json.dumps(found)[:400]}"


def test_a_callable_router_needs_no_branches() -> None:
    """The empty-list rule is the json_path router's (``_JsonPathRouter.branches`` has ``min_length=1``); a callable router names a function instead."""
    draft = {"nodes": [_BEGIN, _agent("a"), _END], "edges": [_edge("s", "a"), {"kind": "conditional", "from_node": "a", "router": {"kind": "callable", "callable_id": "pick"}}]}
    assert "branches_none" not in [row["code"] for row in _validate(draft)["blocking"]]


# ---------------------------------------------------------------------------
# a pasted spec (was applyImportedSpec in the deleted editor): the shape is checked before the draft is touched
# ---------------------------------------------------------------------------


def _import(spec: dict | list | None) -> dict:
    """Run ``GB_applyImport(spec, dispatch)`` in V8 with a recording dispatch: ``{"thrown": <string or None>, "dispatched": [...]}``."""
    ctx = _ctx()
    return json.loads(ctx.eval(
        "(function () { var calls = []; var thrown = null;"
        f" try {{ GB_applyImport({json.dumps(spec)}, function (a) {{ calls.push(a); }}); }} catch (e) {{ thrown = e; }}"
        " return JSON.stringify({ thrown: thrown, dispatched: calls }); })()"
    ))


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"nodes": {}}, "`nodes` must be an array."),
        ({"nodes": [], "edges": {}}, "`edges` must be an array when present."),
        ({"nodes": [None]}, "Every entry of `nodes` must be an object."),
        ({"nodes": [], "edges": [3]}, "Every entry of `edges` must be an object."),
        ([], "Spec must be a JSON object with nodes and edges."),
        (None, "Spec must be a JSON object with nodes and edges."),
        ({"edges": []}, "`nodes` must be an array."),
    ],
)
def test_a_spec_with_the_wrong_shape_is_refused_with_a_message_and_the_draft_is_not_touched(spec, message: str) -> None:
    result = _import(spec)
    assert result["thrown"] == message and result["dispatched"] == []


def test_a_spec_with_the_right_shape_is_dispatched_as_it_is() -> None:
    spec = {"description": "d", "nodes": [{"kind": "begin", "id": "s"}], "edges": []}
    result = _import(spec)
    assert result["thrown"] is None and result["dispatched"] == [{"type": "IMPORT_SPEC", "spec": spec}]


def test_without_the_check_a_spec_with_nodes_as_an_object_crashes_the_render_path() -> None:
    """Why the check exists: the reducer spreads whatever it is given, and the validator and the dirty check then throw during render (the console has no error boundary)."""
    ctx = _ctx()
    ctx.eval('var d = GB_reducer({id: "g", nodes: [], edges: []}, {type: "IMPORT_SPEC", spec: {nodes: {}}});')
    assert ctx.eval("(function () { try { GB_validate(d, {}); return false; } catch (e) { return true; } })()") is True


def test_the_import_modal_hands_the_spec_to_the_check_before_it_closes() -> None:
    builder = _src("graph-builder.jsx")
    call = builder[builder.index("<GR_ImportSpecModal"):]
    handler = call[call.index("onApply="):call.index("/>")]
    assert "GB_applyImport(spec, dispatch)" in handler
    assert handler.index("GB_applyImport(spec, dispatch)") < handler.index("setImportOpen(false)"), "a refused spec must leave the modal open to show its message"
