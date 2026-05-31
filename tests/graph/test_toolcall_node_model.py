"""_ToolCallNode model shape — tool_id, arguments dict, optional template override."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.graph import _ToolCallNode


def test_minimal_toolcall() -> None:
    n = _ToolCallNode(id="t", tool_id="workspace__read")
    assert n.kind == "tool_call"
    assert n.tool_id == "workspace__read"
    assert n.arguments == {}
    assert n.arguments_template is None
    assert n.output_schema is None


def test_with_arguments_dict() -> None:
    n = _ToolCallNode(
        id="t",
        tool_id="web__search",
        arguments={"query": "{{ nodes.planner.parsed.q }}", "limit": 10},
    )
    assert n.arguments["query"] == "{{ nodes.planner.parsed.q }}"
    assert n.arguments["limit"] == 10


def test_with_arguments_template_override() -> None:
    n = _ToolCallNode(
        id="t",
        tool_id="web__search",
        arguments_template='{"q": "{{ nodes.x.text }}", "k": 5}',
    )
    assert n.arguments_template is not None
    assert n.arguments == {}


def test_with_output_schema() -> None:
    n = _ToolCallNode(
        id="t",
        tool_id="web__search",
        output_schema={"type": "object", "required": ["results"]},
    )
    assert n.output_schema is not None


def test_tool_id_required() -> None:
    with pytest.raises(ValidationError):
        _ToolCallNode(id="t", tool_id="")
