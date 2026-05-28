"""Tests for matrix.graph.template.render_input_template."""

from __future__ import annotations

import pytest

from primer.graph.template import render_input_template
from primer.model.chat import Message, TextPart
from primer.model.except_ import BadRequestError
from primer.model.graph import GraphContext, NodeOutput


def _ctx(
    *,
    initial_input: list[Message] | None = None,
    iteration: int = 0,
    nodes: dict[str, NodeOutput] | None = None,
) -> GraphContext:
    return GraphContext(
        initial_input=initial_input
        or [Message(role="user", parts=[TextPart(text="hello")])],
        iteration=iteration,
        nodes=nodes or {},
    )


class TestRender:
    def test_static_template(self) -> None:
        result = render_input_template("just a static string", context=_ctx())
        assert result == "just a static string"

    def test_default_template_renders_initial_input(self) -> None:
        ctx = _ctx(
            initial_input=[
                Message(role="user", parts=[TextPart(text="line one")]),
                Message(role="user", parts=[TextPart(text="line two")]),
            ]
        )
        template = (
            "{% for m in initial_input %}{{ m.parts[0].text }}\n{% endfor %}"
        )
        result = render_input_template(template, context=ctx)
        assert "line one" in result
        assert "line two" in result

    def test_node_text_access(self) -> None:
        ctx = _ctx(
            nodes={"A": NodeOutput(text="A's reply", iteration=0)},
        )
        result = render_input_template("Got: {{ nodes.A.text }}", context=ctx)
        assert result == "Got: A's reply"

    def test_node_parsed_access(self) -> None:
        ctx = _ctx(
            nodes={
                "D": NodeOutput(
                    text="{...}",
                    parsed={"next_action": "retry", "reason": "low confidence"},
                    iteration=1,
                )
            },
        )
        result = render_input_template(
            "Action: {{ nodes.D.parsed.next_action }}", context=ctx
        )
        assert result == "Action: retry"

    def test_iteration_variable(self) -> None:
        ctx = _ctx(iteration=3)
        result = render_input_template("Iteration {{ iteration }}", context=ctx)
        assert result == "Iteration 3"

    def test_fan_in_composition(self) -> None:
        ctx = _ctx(
            nodes={
                "B": NodeOutput(text="from B", iteration=0),
                "C": NodeOutput(text="from C", iteration=0),
            },
        )
        result = render_input_template(
            "B says: {{ nodes.B.text }}\nC says: {{ nodes.C.text }}",
            context=ctx,
        )
        assert "from B" in result
        assert "from C" in result


class TestErrors:
    def test_syntax_error_raises_bad_request(self) -> None:
        with pytest.raises(BadRequestError, match="syntax error"):
            render_input_template("{{ unbalanced", context=_ctx())

    def test_missing_variable_raises_bad_request(self) -> None:
        with pytest.raises(BadRequestError, match="render error"):
            render_input_template("{{ no_such_var }}", context=_ctx())

    def test_missing_node_raises_bad_request(self) -> None:
        with pytest.raises(BadRequestError, match="render error"):
            render_input_template(
                "{{ nodes.nonexistent.text }}", context=_ctx()
            )

    def test_sandbox_blocks_dunder_access(self) -> None:
        with pytest.raises(BadRequestError):
            render_input_template(
                "{{ ''.__class__ }}", context=_ctx()
            )
