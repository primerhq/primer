"""_resolve_toolcall_arguments walks an arguments dict, rendering string
leaves as Jinja against GraphContext, passing non-strings through.
Optional arguments_template overrides the dict."""

from __future__ import annotations

import pytest

from primer.model.graph import GraphContext, NodeOutput, _ToolCallNode
from primer.graph.base import _resolve_toolcall_arguments


def _ctx() -> GraphContext:
    return GraphContext(
        initial_input="",
        iteration=0,
        nodes={
            "planner": NodeOutput(
                text="",
                parsed={"q": "hello world", "n": 5},
                iteration=0,
            ),
        },
    )


def test_per_leaf_string_jinja() -> None:
    node = _ToolCallNode(
        id="t",
        tool_id="web__search",
        arguments={"query": "{{ nodes.planner.parsed.q }}", "limit": 10},
    )
    args = _resolve_toolcall_arguments(node, _ctx())
    assert args == {"query": "hello world", "limit": 10}


def test_non_string_leaves_pass_through() -> None:
    node = _ToolCallNode(
        id="t",
        tool_id="web__search",
        arguments={"k": 5, "flag": True, "nested": {"a": 1}},
    )
    args = _resolve_toolcall_arguments(node, _ctx())
    assert args == {"k": 5, "flag": True, "nested": {"a": 1}}


def test_arguments_template_override() -> None:
    node = _ToolCallNode(
        id="t",
        tool_id="web__search",
        arguments={"ignored": "yes"},
        arguments_template='{"q": "{{ nodes.planner.parsed.q }}", "n": {{ nodes.planner.parsed.n }}}',
    )
    args = _resolve_toolcall_arguments(node, _ctx())
    assert args == {"q": "hello world", "n": 5}


def test_template_jinja_error_raises() -> None:
    node = _ToolCallNode(
        id="t",
        tool_id="web__search",
        arguments={"q": "{{ nodes.absent.text }}"},  # StrictUndefined error
    )
    with pytest.raises(Exception):
        _resolve_toolcall_arguments(node, _ctx())


def test_arguments_template_json_parse_error_raises() -> None:
    node = _ToolCallNode(
        id="t",
        tool_id="web__search",
        arguments_template='not json{}',
    )
    with pytest.raises(Exception):
        _resolve_toolcall_arguments(node, _ctx())
