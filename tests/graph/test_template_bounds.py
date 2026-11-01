"""Graph templates render bounded and off the event loop (AG-M23, ruling D8: 10 s, 1 MiB, a repetition bound)."""

from __future__ import annotations

import asyncio
import time

import pytest
from jinja2 import TemplateError

from primer.graph.base import _render_end_output, _render_fanin_output
from primer.graph.template import render_input_template, render_template_safely
from primer.model.except_ import BadRequestError
from primer.model.graph import GraphContext, NodeOutput, _EndNode, _FanInNode


def _ctx() -> GraphContext:
    return GraphContext(
        initial_input="in", iteration=2,
        nodes={"A": NodeOutput(text='[{"u": 1}]', parsed=None, history=[], iteration=0)},
    )


# --- characterization: renders and errors are what they were ---

_FIXTURE = (
    "{{ '-' * 3 }}|{{ 2 ** 10 }}|{{ [0] * 2 }}|{{ (nodes.A.text | fromjson)[0].u }}|{{ iteration * 2 }}"
    "|{% for k in nodes %}{{ k }}{% endfor %}\n"
)


def test_fixture_templates_render_byte_identically() -> None:
    assert render_input_template(_FIXTURE, context=_ctx()) == "---|1024|[0, 0]|1|4|A\n"
    assert render_template_safely(_FIXTURE, _ctx(), extra_scope={"fanout_index": 1}) == "---|1024|[0, 0]|1|4|A\n"


@pytest.mark.parametrize(("template", "input_message", "safe_type", "safe_message"), [
    ("{{ nope }}", "render_input_template: render error: 'nope' is undefined", "UndefinedError", "'nope' is undefined"),
    ("{% for %}", "render_input_template: template syntax error at line 1: Expected an expression, got 'end of statement block'",
     "TemplateSyntaxError", "Expected an expression, got 'end of statement block'"),
    ("{{ ''.__class__ }}", "render_input_template: render error: access to attribute '__class__' of 'str' object is unsafe.",
     "SecurityError", "access to attribute '__class__' of 'str' object is unsafe."),
])
def test_template_errors_keep_type_and_message(template, input_message, safe_type, safe_message) -> None:
    with pytest.raises(BadRequestError) as exc:
        render_input_template(template, context=_ctx())
    assert str(exc.value) == input_message
    with pytest.raises(TemplateError) as raw:
        render_template_safely(template, _ctx())
    assert (type(raw.value).__name__, str(raw.value)) == (safe_type, safe_message)


def test_a_type_error_still_escapes_as_itself() -> None:
    with pytest.raises(TypeError, match="can't multiply sequence by non-int of type 'str'"):
        render_input_template("{{ 'a' * 'b' }}", context=_ctx())


async def test_end_output_template_error_code_is_unchanged() -> None:
    res = await _render_end_output(_EndNode(id="end", output_template="{{ nope }}"), _ctx())
    assert (res.error_code, res.error_message) == ("template_error", "'nope' is undefined")


# --- AG-M23: the bounds ---


@pytest.mark.parametrize("template", ["{{ 'x' * 2000000 }}", "{{ ('x' * 200000000) | length }}", "{{ 10 ** 10000000 }}"])
def test_a_huge_repetition_or_power_is_refused(template: str) -> None:
    with pytest.raises(TemplateError, match="limit"):
        render_template_safely(template, _ctx())
    with pytest.raises(BadRequestError, match="limit"):
        render_input_template(template, context=_ctx())


def test_output_past_one_mib_is_refused() -> None:
    with pytest.raises(TemplateError, match="1048576 character limit"):
        render_template_safely("{% for i in range(1025) %}{{ 'x' * 1024 }}{% endfor %}", _ctx())


def test_the_probe_template_is_stopped_at_the_time_limit(monkeypatch) -> None:
    from primer.common import bounded_template

    monkeypatch.setattr(bounded_template, "RENDER_SECONDS", 0.2)
    started = time.monotonic()
    with pytest.raises(TemplateError, match="time limit"):
        render_template_safely("{% for i in range(100000) %}{% for j in range(300) %}{% endfor %}{% endfor %}x", _ctx())
    assert time.monotonic() - started < 2.0


async def _ticks_while(coro):
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        result = await coro
    finally:
        task.cancel()
    return ticks, result


async def test_an_oversized_end_template_fails_template_error_and_the_loop_keeps_ticking(monkeypatch) -> None:
    from primer.common import bounded_template

    monkeypatch.setattr(bounded_template, "RENDER_SECONDS", 0.5)
    slow = "{% for a in range(4000) %}{% for b in range(4000) %}{% endfor %}{% endfor %}"
    ticks, res = await _ticks_while(_render_end_output(_EndNode(id="end", output_template=slow), _ctx()))
    assert res.error_code == "template_error" and "time limit" in res.error_message
    assert ticks >= 10, ticks
    ticks, fres = await _ticks_while(
        _render_fanin_output(_FanInNode(id="fi", aggregate_template=slow), _ctx()),
    )
    assert fres.error_code == "template_error"
    assert ticks >= 10, ticks
