"""Graph-builder foundations (ui/graph-builder/WIRING.md §3, §7, §10).

These modules are pure logic, so they are executed for real in MiniRacer
rather than substring-matched: the draft reducer (including RENAME_NODE's
reference rewriting), the Jinja <-> chip parser/serialiser (whose round-trip
must be lossless), superstep layering, and the two validation tiers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
GB = UI / "components" / "graph-builder"
MODEL = GB / "gb-model.jsx"
REFS = GB / "gb-refs.jsx"
VALIDATE = GB / "gb-validate.jsx"
API = GB / "gb-api.jsx"


def _ctx():
    """A MiniRacer context with the foundation modules loaded.

    The modules attach their exports to `window`; the shim mirrors them onto
    globalThis so tests can call them unqualified, exactly as the browser
    does via the global <script> bundle.
    """
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis; window.primerApi = {};")
    for path in (MODEL, REFS, VALIDATE):
        ctx.eval(path.read_text(encoding="utf-8"))
    return ctx


def test_modules_exist_and_export() -> None:
    for path in (MODEL, REFS, VALIDATE, API):
        assert path.exists(), f"{path} is missing"
    model = MODEL.read_text(encoding="utf-8")
    assert "function GB_reducer(" in model
    assert "function GB_makeNode(" in model
    assert "function GB_supersteps(" in model
    refs = REFS.read_text(encoding="utf-8")
    assert "function GB_parseTemplate(" in refs
    assert "function GB_serialize(" in refs
    assert "function GB_renameInTemplates(" in refs
    assert "function GB_validate(" in VALIDATE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Node factory - the fix for "nodes are born broken"
# ---------------------------------------------------------------------------


def test_make_node_is_complete_and_named_from_the_label() -> None:
    ctx = _ctx()
    ctx.eval(
        'var n = GB_makeNode({kind:"agent", agentId:"editor", '
        'label:"Review the draft", upstreamId:"draft_1"});'
    )
    assert ctx.eval("n.id") == "review_the_draft"
    assert ctx.eval("n.description") == "Review the draft"
    assert ctx.eval("n.agent_id") == "editor"
    # It is wired to its upstream step, not left empty.
    assert ctx.eval("n.input_template") == "{{ nodes.draft_1.text }}"


def test_make_node_dedupes_ids() -> None:
    ctx = _ctx()
    ctx.eval('var n = GB_makeNode({kind:"agent", label:"Review", takenIds:["review"]});')
    assert ctx.eval("n.id") == "review_2"


def test_split_creates_both_halves_and_the_merge_edge() -> None:
    # A fan_in with no incoming edge is a persist-time violation, so the
    # palette's "split the work" must create the pair wired together.
    ctx = _ctx()
    ctx.eval('var p = GB_makeSplitPair({agentId:"writer"});')
    assert ctx.eval("p.nodes.length") == 3
    assert ctx.eval('p.nodes.map(function(n){return n.kind}).join(",")') == "fan_out,agent,fan_in"
    assert ctx.eval("p.edges.length") == 1
    assert ctx.eval("p.edges[0].from_node === p.nodes[1].id") is True
    assert ctx.eval("p.edges[0].to_node === p.nodes[2].id") is True
    # The fan-out targets the worker through its spec, never through an edge.
    assert ctx.eval("p.nodes[0].specs[0].target_node_id === p.nodes[1].id") is True


# ---------------------------------------------------------------------------
# RENAME_NODE - rewrites every reference (the reason labels can be primary)
# ---------------------------------------------------------------------------


def test_rename_rewrites_edges_routers_fanout_specs_and_templates() -> None:
    ctx = _ctx()
    ctx.eval(
        """
        var draft = {
          nodes: [
            {kind:"begin", id:"start"},
            {kind:"agent", id:"draft_1", description:"Draft", input_template:"{{ initial_input }}"},
            {kind:"agent", id:"review_1", description:"Review",
             input_template:"Check this:\\n{{ nodes.draft_1.text }} and {{ nodes['draft_1'].parsed.x }}"},
            {kind:"fan_out", id:"split", specs:[
              {kind:"map", target_node_id:"draft_1", source_node_id:"draft_1", source_path:"items"}]},
            {kind:"end", id:"done", output_template:"{{ nodes.draft_1.text }}"}
          ],
          edges: [
            {kind:"static", from_node:"start", to_node:"draft_1"},
            {kind:"conditional", from_node:"review_1", router:{kind:"json_path",
              branches:[{conditions:[], to_node:"draft_1"}], default_to:"draft_1"}}
          ],
          on_max_iterations: "draft_1"
        };
        var out = GB_reducer(draft, {type:"RENAME_NODE", id:"draft_1", newId:"write_post",
                                     newDescription:"Write the post"});
        """
    )
    # The node itself.
    assert ctx.eval('out.nodes[1].id') == "write_post"
    assert ctx.eval('out.nodes[1].description') == "Write the post"
    # Edges + router branches + default_to.
    assert ctx.eval('out.edges[0].to_node') == "write_post"
    assert ctx.eval('out.edges[1].router.branches[0].to_node') == "write_post"
    assert ctx.eval('out.edges[1].router.default_to') == "write_post"
    # Fan-out spec targets AND the map source.
    assert ctx.eval('out.nodes[3].specs[0].target_node_id') == "write_post"
    assert ctx.eval('out.nodes[3].specs[0].source_node_id') == "write_post"
    # Graph-level landing node.
    assert ctx.eval('out.on_max_iterations') == "write_post"
    # Jinja templates - both dotted and bracket forms, in every template field.
    assert ctx.eval('out.nodes[2].input_template').count("write_post") == 2
    assert "draft_1" not in ctx.eval('out.nodes[2].input_template')
    assert ctx.eval('out.nodes[4].output_template') == "{{ nodes.write_post.text }}"


def test_delete_node_cleans_up_dangling_references() -> None:
    ctx = _ctx()
    ctx.eval(
        """
        var draft = {
          nodes: [{kind:"agent", id:"a"}, {kind:"agent", id:"b"},
                  {kind:"fan_out", id:"s", specs:[{kind:"tee", target_node_ids:["a","b"]}]}],
          edges: [{kind:"static", from_node:"a", to_node:"b"}],
          on_max_iterations: "a"
        };
        var out = GB_reducer(draft, {type:"DELETE_NODE", id:"a"});
        """
    )
    assert ctx.eval("out.nodes.length") == 2
    assert ctx.eval("out.edges.length") == 0
    assert ctx.eval('out.nodes[1].specs[0].target_node_ids.join(",")') == "b"
    assert ctx.eval("out.on_max_iterations") is None


# ---------------------------------------------------------------------------
# Reference chips - round-tripping MUST be lossless
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "plain text only",
        "Draft: {{ nodes.draft_1.text }}",
        "{{ initial_input }} and {{ initial_input.topic }}",
        "{{ nodes['worker[0]'].text }}",
        "{% for o in nodes.worker %}{{ o.text }}{% endfor %}",
        "{{ nodes.a.text | upper }}",
        "{{  nodes.a.text  }}",  # non-canonical spacing must survive
        "mixed {{ nodes.a.parsed.x.y }} then {% if x %}y{% endif %} end",
        "",
    ],
)
def test_template_round_trip_is_lossless(template: str) -> None:
    # A graph authored in raw JSON must survive an open/save in the builder
    # byte-for-byte (WIRING §7).
    ctx = _ctx()
    ctx.eval("var src = " + _js(template) + ";")
    ctx.eval("var out = GB_serialize(GB_parseTemplate(src));")
    assert ctx.eval("out === src") is True, ctx.eval("out")


def _js(value: str) -> str:
    import json

    return json.dumps(value)


def test_simple_refs_become_chips_and_complex_stays_raw() -> None:
    ctx = _ctx()
    ctx.eval(
        'var t = GB_parseTemplate("Hi {{ nodes.draft_1.text }} '
        '{% for x in y %}{{ x }}{% endfor %} {{ a.b | upper }}");'
    )
    kinds = ctx.eval('t.map(function(x){return x.t}).join(",")')
    assert "ref" in kinds
    # The loop statement and the filtered expression must NOT become chips.
    assert ctx.eval('t.filter(function(x){return x.t==="ref"}).length') == 1
    assert ctx.eval('t.filter(function(x){return x.t==="ref"})[0].nodeId') == "draft_1"
    assert ctx.eval('t.filter(function(x){return x.t==="ref"})[0].path') == "text"
    assert ctx.eval('t.filter(function(x){return x.t==="raw"}).length') >= 2


def test_chip_label_uses_the_upstream_description_not_the_id() -> None:
    ctx = _ctx()
    ctx.eval(
        """
        var draft = {nodes:[{kind:"begin", id:"start", description:"Start"},
                            {kind:"agent", id:"draft_1", description:"Draft the post"}]};
        var tok = GB_parseTemplate("{{ nodes.draft_1.text }}")[0];
        var label = GB_chipLabel(draft, tok);
        var broken = GB_refIsBroken(draft, GB_parseTemplate("{{ nodes.gone.text }}")[0]);
        """
    )
    assert ctx.eval("label") == "Draft the post · text"
    assert ctx.eval("broken") is True


# ---------------------------------------------------------------------------
# Supersteps
# ---------------------------------------------------------------------------


def test_supersteps_layer_parallel_work_together() -> None:
    ctx = _ctx()
    ctx.eval(
        """
        var draft = {
          nodes: [{kind:"begin", id:"s"}, {kind:"agent", id:"a"}, {kind:"agent", id:"b"},
                  {kind:"fan_in", id:"m"}, {kind:"end", id:"e"}],
          edges: [{kind:"static", from_node:"s", to_node:"a"},
                  {kind:"static", from_node:"s", to_node:"b"},
                  {kind:"static", from_node:"a", to_node:"m"},
                  {kind:"static", from_node:"b", to_node:"m"},
                  {kind:"static", from_node:"m", to_node:"e"}]
        };
        var L = GB_supersteps(draft);
        """
    )
    assert ctx.eval("L.length") == 4
    assert ctx.eval('L[0].join(",")') == "s"
    # a and b are ready at the same time - that is the point of the bands.
    assert sorted(ctx.eval('L[1].join(",")').split(",")) == ["a", "b"]
    assert ctx.eval('L[2].join(",")') == "m"


def test_supersteps_terminate_on_a_cycle() -> None:
    ctx = _ctx()
    ctx.eval(
        """
        var draft = {nodes:[{kind:"agent", id:"a"}, {kind:"agent", id:"b"}],
                     edges:[{kind:"static", from_node:"a", to_node:"b"},
                            {kind:"static", from_node:"b", to_node:"a"}]};
        var L = GB_supersteps(draft);
        """
    )
    assert ctx.eval("L.length") >= 1  # terminates rather than hanging


# ---------------------------------------------------------------------------
# Validation tiers - drafts save, only runs are blocked
# ---------------------------------------------------------------------------


def test_partial_draft_saves_but_does_not_run() -> None:
    # The whole point of the "Draft" chip: an incomplete graph is legitimate.
    ctx = _ctx()
    ctx.eval(
        """
        var draft = {nodes:[{kind:"agent", id:"a", description:"A", agent_id:"x"}], edges:[]};
        var v = GB_validate(draft, {});
        """
    )
    assert ctx.eval("v.blocking.length") == 0, "a partial draft must still save"
    codes = ctx.eval('v.runnable.map(function(r){return r.code}).join(",")')
    assert "begin_count" in codes
    assert "no_end" in codes


def test_referential_breakage_blocks_save() -> None:
    ctx = _ctx()
    ctx.eval(
        """
        var draft = {
          nodes:[{kind:"begin", id:"s"}, {kind:"fan_out", id:"f", specs:[]},
                 {kind:"agent", id:"a", agent_id:"x", description:"A"}],
          edges:[{kind:"static", from_node:"f", to_node:"a"},
                 {kind:"static", from_node:"a", to_node:"ghost"}]
        };
        var v = GB_validate(draft, {});
        var codes = v.blocking.map(function(b){return b.code});
        """
    )
    assert ctx.eval('codes.indexOf("fanout_has_edge") >= 0') is True
    assert ctx.eval('codes.indexOf("unknown_target") >= 0') is True


def test_branch_without_response_format_and_missing_catch_all_are_runnable_tier() -> None:
    # The two traps the brief called out, surfaced before the run fails.
    ctx = _ctx()
    ctx.eval(
        """
        var draft = {
          nodes:[{kind:"begin", id:"s"},
                 {kind:"agent", id:"rev", description:"Review", agent_id:"editor"},
                 {kind:"end", id:"e"}],
          edges:[{kind:"static", from_node:"s", to_node:"rev"},
                 {kind:"conditional", from_node:"rev", router:{kind:"json_path",
                   branches:[{conditions:[{path:"approved", op:"eq", value:true}], to_node:"e"}]}}]
        };
        var v = GB_validate(draft, {});
        var codes = v.runnable.map(function(r){return r.code});
        """
    )
    assert ctx.eval('codes.indexOf("branch_requires_response_format") >= 0') is True
    assert ctx.eval('codes.indexOf("no_catch_all") >= 0') is True
    assert ctx.eval("v.blocking.length") == 0  # still saveable


def test_unbounded_cycle_blocks_running_only() -> None:
    ctx = _ctx()
    ctx.eval(
        """
        var draft = {
          nodes:[{kind:"begin", id:"s"}, {kind:"agent", id:"a", agent_id:"x"},
                 {kind:"agent", id:"b", agent_id:"y"}, {kind:"end", id:"e"}],
          edges:[{kind:"static", from_node:"s", to_node:"a"},
                 {kind:"static", from_node:"a", to_node:"b"},
                 {kind:"static", from_node:"b", to_node:"a"},
                 {kind:"static", from_node:"a", to_node:"e"}]
        };
        var v = GB_validate(draft, {});
        var codes = v.runnable.map(function(r){return r.code});
        """
    )
    assert ctx.eval('codes.indexOf("unbounded_loop") >= 0') is True
    assert ctx.eval("v.blocking.length") == 0
    # Setting a cap clears it.
    ctx.eval("draft.max_iterations = 3; var v2 = GB_validate(draft, {});")
    assert ctx.eval('v2.runnable.map(function(r){return r.code}).indexOf("unbounded_loop")') == -1


def test_every_failure_code_has_plain_language_copy() -> None:
    # The run drawer and the session view must speak one language (§10).
    ctx = _ctx()
    for code in (
        "max_iterations_exceeded", "routing_failed", "template_error",
        "tool_execution_failed", "fanout_source_invalid", "end_output_invalid",
        "tool_output_invalid", "fanin_upstream_failed",
    ):
        assert ctx.eval(f'typeof GB_FAILURE_COPY["{code}"]') == "string"
        assert len(ctx.eval(f'GB_FAILURE_COPY["{code}"]')) > 20
