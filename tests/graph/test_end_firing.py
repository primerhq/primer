"""End firing renders output_template, validates output_schema, and
populates the executor's terminal sentinel + ended_detail on failure."""

from __future__ import annotations

import pytest

from primer.graph.base import _EndOutputResult, _render_end_output
from primer.model.graph import GraphContext, NodeOutput, _EndNode


def _ctx_with(node_id: str, parsed) -> GraphContext:
    return GraphContext(
        initial_input=[],
        iteration=0,
        nodes={
            node_id: NodeOutput(
                text=str(parsed), parsed=parsed, history=[], iteration=0
            )
        },
    )


def test_empty_template_returns_empty_text_no_parsed() -> None:
    end = _EndNode(id="end")
    res = _render_end_output(end, _ctx_with("x", {}))
    assert res.text == ""
    assert res.parsed is None
    assert res.error_code is None


def test_template_renders_and_no_schema_passes() -> None:
    end = _EndNode(id="end", output_template="{{ nodes.x.parsed.summary }}")
    res = _render_end_output(end, _ctx_with("x", {"summary": "all done"}))
    assert res.text == "all done"
    assert res.parsed is None
    assert res.error_code is None


def test_schema_validates_parsed_json() -> None:
    end = _EndNode(
        id="end",
        output_template='{"summary": "{{ nodes.x.parsed.summary }}"}',
        output_schema={
            "type": "object",
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
        },
    )
    res = _render_end_output(end, _ctx_with("x", {"summary": "ok"}))
    assert res.parsed == {"summary": "ok"}
    assert res.error_code is None


def test_schema_rejects_invalid_json() -> None:
    end = _EndNode(
        id="end",
        output_template='{"summary": "{{ nodes.x.parsed.summary }}"}',
        output_schema={
            "type": "object",
            "required": ["summary"],
            "properties": {"summary": {"type": "integer"}},
        },
    )
    res = _render_end_output(end, _ctx_with("x", {"summary": "not_an_int"}))
    assert res.error_code == "end_output_invalid"


def test_template_undefined_variable_sets_template_error() -> None:
    end = _EndNode(id="end", output_template="{{ nodes.absent.text }}")
    res = _render_end_output(end, _ctx_with("x", {"a": 1}))
    assert res.error_code == "template_error"


def test_schema_without_json_parse_yields_end_output_invalid() -> None:
    end = _EndNode(
        id="end",
        output_template="just plain text",
        output_schema={"type": "object"},
    )
    res = _render_end_output(end, _ctx_with("x", {}))
    assert res.error_code == "end_output_invalid"
