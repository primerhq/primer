"""When the executor renders a fan-out instance's template, fanout_index
and fanout_item are in the Jinja scope (Spec B §2.1)."""

from __future__ import annotations

from primer.model.graph import GraphContext, NodeOutput
from primer.graph.template import render_template_safely


def _ctx() -> GraphContext:
    return GraphContext(
        initial_input="seed",
        iteration=0,
        nodes={"begin": NodeOutput(text="x", iteration=0)},
    )


def test_fanout_index_in_scope() -> None:
    out = render_template_safely(
        "Worker #{{ fanout_index }}",
        _ctx(),
        extra_scope={"fanout_index": 2, "fanout_item": "raw"},
    )
    assert out == "Worker #2"


def test_fanout_item_in_scope() -> None:
    out = render_template_safely(
        "{{ fanout_item.title }}",
        _ctx(),
        extra_scope={"fanout_index": 0, "fanout_item": {"title": "Chapter One"}},
    )
    assert out == "Chapter One"


def test_existing_context_vars_still_in_scope() -> None:
    out = render_template_safely(
        "{{ nodes.begin.text }}/{{ fanout_index }}",
        _ctx(),
        extra_scope={"fanout_index": 7, "fanout_item": None},
    )
    assert out == "x/7"


def test_no_extra_scope_means_no_fanout_vars() -> None:
    """Backwards compat: existing callers that don't pass extra_scope continue
    to work without exposing fanout_* vars."""
    out = render_template_safely(
        "{{ nodes.begin.text }}",
        _ctx(),
    )
    assert out == "x"
