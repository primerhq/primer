"""Tests for primer.graph.template.render_input_template."""

from __future__ import annotations

import pytest

from primer.graph.template import render_input_template, render_template_safely
from primer.model.chat import Message, TextPart
from primer.model.except_ import BadRequestError
from primer.model.graph import GraphContext, NodeOutput, build_execution_context


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


class TestFromJsonFilter:
    """The ``fromjson`` filter lets a node template over a tool_call node's
    JSON ``text`` output -- e.g. web_search returns a top-level JSON array,
    whose ``.parsed`` stays None (NodeOutput.parsed is dict-only), so the
    only way to reach ``[0].url`` is to parse the text in the template."""

    def test_parses_json_array_and_indexes(self) -> None:
        ctx = _ctx(
            nodes={
                "search": NodeOutput(
                    text='[{"url": "https://a.example/x", "title": "X"}, '
                    '{"url": "https://b.example/y", "title": "Y"}]',
                    iteration=0,
                )
            },
        )
        result = render_input_template(
            "First: {{ (nodes.search.text | fromjson)[0].url }}", context=ctx
        )
        assert result == "First: https://a.example/x"

    def test_parses_json_object(self) -> None:
        ctx = _ctx(
            nodes={"t": NodeOutput(text='{"k": "v"}', iteration=0)},
        )
        result = render_input_template(
            "{{ (nodes.t.text | fromjson).k }}", context=ctx
        )
        assert result == "v"

    def test_idempotent_on_already_parsed(self) -> None:
        # A non-string (already-parsed) value passes through unchanged.
        ctx = _ctx(
            nodes={
                "p": NodeOutput(
                    text="{...}", parsed={"items": [1, 2, 3]}, iteration=0
                )
            },
        )
        result = render_input_template(
            "{{ (nodes.p.parsed | fromjson)['items'][2] }}", context=ctx
        )
        assert result == "3"

    def test_invalid_json_raises_bad_request(self) -> None:
        ctx = _ctx(nodes={"bad": NodeOutput(text="not json{", iteration=0)})
        with pytest.raises(BadRequestError, match="render error"):
            render_input_template(
                "{{ (nodes.bad.text | fromjson)[0] }}", context=ctx
            )


class TestStripFencesFilter:
    """The ``strip_fences`` filter sanitises a model's code output before it is
    written to a file — local models habitually wrap code in ```lang fences
    (and add prose around it), which breaks a downstream ``python3`` run."""

    def test_extracts_fenced_block_dropping_prose(self) -> None:
        ctx = _ctx(
            nodes={
                "code": NodeOutput(
                    text="Here is the file:\n```python\nprint('hi')\n```\nDone.",
                    iteration=0,
                )
            },
        )
        result = render_input_template(
            "{{ nodes.code.text | strip_fences }}", context=ctx
        )
        assert result == "print('hi')"

    def test_idempotent_on_raw_code(self) -> None:
        raw = "import sys\nprint(sys.argv)"
        ctx = _ctx(nodes={"code": NodeOutput(text=raw, iteration=0)})
        result = render_input_template(
            "{{ nodes.code.text | strip_fences }}", context=ctx
        )
        assert result == raw

    def test_strips_stray_unclosed_fence_marker_lines(self) -> None:
        # No closing fence: the bare ```python marker line must be removed so
        # the remaining text is valid source.
        ctx = _ctx(
            nodes={"code": NodeOutput(text="```python\nx = 1\ny = 2", iteration=0)},
        )
        result = render_input_template(
            "{{ nodes.code.text | strip_fences }}", context=ctx
        )
        assert result == "x = 1\ny = 2"


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


class TestCtx:
    def test_default_ctx_surface_is_memory(self) -> None:
        result = render_input_template("s={{ ctx.surface }}", context=_ctx())
        assert result == "s=memory"

    def test_default_ctx_artifact_dir_is_none_renders_none(self) -> None:
        # Null field must render the string "None", NOT raise (StrictUndefined
        # only fires on missing *names*, not on defined-but-None values).
        result = render_input_template(
            "dir={{ ctx.artifact_dir }}", context=_ctx()
        )
        assert result == "dir=None"

    def test_ctx_artifact_dir_renders_when_set(self) -> None:
        ctx = GraphContext(
            initial_input=[Message(role="user", parts=[TextPart(text="hi")])],
            iteration=0,
            nodes={},
            ctx=build_execution_context(surface="workspace", session_id="gsid-1"),
        )
        result = render_input_template(
            "write {{ ctx.artifact_dir }}/plan.md", context=ctx
        )
        assert result == "write artifacts/gsid-1/plan.md"

    def test_ctx_typo_field_still_raises(self) -> None:
        with pytest.raises(BadRequestError):
            render_input_template("{{ ctx.artifactdir }}", context=_ctx())

    def test_ctx_available_in_render_template_safely(self) -> None:
        ctx = GraphContext(
            initial_input=[Message(role="user", parts=[TextPart(text="hi")])],
            iteration=0,
            nodes={},
            ctx=build_execution_context(surface="workspace", session_id="gsid-2"),
        )
        result = render_template_safely("{{ ctx.artifact_dir }}", ctx)
        assert result == "artifacts/gsid-2"


def test_node_template_preserves_trailing_newline() -> None:
    ctx = GraphContext(initial_input=[], iteration=0, nodes={})
    assert render_input_template("hello\n", context=ctx) == "hello\n"
