"""FanIn firing renders aggregate_template against GraphContext;
output_schema validates the rendered JSON.

Mirrors tests/graph/test_end_firing.py shape — focuses on the
:func:`_render_fanin_output` helper which the executor calls
when a FanIn node enters the ready set."""

from __future__ import annotations

from primer.model.graph import GraphContext, NodeOutput, _FanInNode
from primer.graph.base import _render_fanin_output


def _ctx_with_workers() -> GraphContext:
    return GraphContext(
        initial_input="",
        iteration=0,
        nodes={
            "worker": [
                NodeOutput(text="w0", parsed={"score": 1}, iteration=0),
                NodeOutput(text="w1", parsed={"score": 2}, iteration=0),
            ],
        },
    )


def test_empty_template_returns_empty_text() -> None:
    fan = _FanInNode(id="join")
    res = _render_fanin_output(fan, _ctx_with_workers())
    assert res.text == ""
    assert res.error_code is None


def test_renders_aggregate_template() -> None:
    fan = _FanInNode(
        id="join",
        aggregate_template="{% for w in nodes.worker %}{{ w.text }} {% endfor %}",
    )
    res = _render_fanin_output(fan, _ctx_with_workers())
    assert res.text.strip() == "w0 w1"


def test_output_schema_validates() -> None:
    fan = _FanInNode(
        id="join",
        aggregate_template='{"sum": {{ (nodes.worker | map(attribute="parsed") | map(attribute="score") | sum) }}}',
        output_schema={
            "type": "object",
            "required": ["sum"],
            "properties": {"sum": {"type": "integer"}},
        },
    )
    res = _render_fanin_output(fan, _ctx_with_workers())
    assert res.parsed == {"sum": 3}
    assert res.error_code is None


def test_output_schema_failure() -> None:
    fan = _FanInNode(
        id="join",
        aggregate_template="not json",
        output_schema={"type": "object"},
    )
    res = _render_fanin_output(fan, _ctx_with_workers())
    assert res.error_code == "end_output_invalid"
