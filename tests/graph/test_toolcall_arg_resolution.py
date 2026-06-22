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


def test_extra_scope_fanout_item_in_arguments() -> None:
    """A ToolCall fan-out (map/broadcast) target must see ``fanout_item`` /
    ``fanout_index`` in its arguments, like the agent + subgraph node paths.

    Without ``extra_scope`` the StrictUndefined renderer raises and every
    synthesized instance fails with ``template_error`` (the compliance-sweep
    map-over-tool_call regression).
    """
    node = _ToolCallNode(
        id="audit",
        tool_id="misc__calculate",
        arguments={
            "expression": "{{ fanout_item.expr }}",
            "idx": "{{ fanout_index }}",
        },
    )
    args = _resolve_toolcall_arguments(
        node, _ctx(),
        extra_scope={"fanout_index": 2, "fanout_item": {"expr": "90 + 5"}},
    )
    assert args == {"expression": "90 + 5", "idx": "2"}


def test_extra_scope_fanout_item_in_arguments_template() -> None:
    node = _ToolCallNode(
        id="audit",
        tool_id="misc__calculate",
        arguments_template='{"expression": "{{ fanout_item.expr }}"}',
    )
    args = _resolve_toolcall_arguments(
        node, _ctx(),
        extra_scope={"fanout_index": 0, "fanout_item": {"expr": "1 + 1"}},
    )
    assert args == {"expression": "1 + 1"}


def test_fanout_item_undefined_without_extra_scope_raises() -> None:
    """Regression guard: a ToolCall referencing ``fanout_item`` with NO
    extra_scope still raises (StrictUndefined) - the fix only adds the scope
    when the node is actually a fan-out instance."""
    node = _ToolCallNode(
        id="audit",
        tool_id="misc__calculate",
        arguments={"expression": "{{ fanout_item.expr }}"},
    )
    with pytest.raises(Exception):
        _resolve_toolcall_arguments(node, _ctx())
