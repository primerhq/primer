"""G6-4: a graph node's output_schema ``pattern`` runs bounded and off the worker's event loop.

The End, FanIn and ToolCall nodes validate their output against the graph author's ``output_schema``. jsonschema ran its
``pattern`` keyword with stdlib ``re`` inside the executor coroutine, so ``{"pattern": "(a|aa)+$"}`` against a 37-character
output stalled the worker's event loop for seconds (the hazard #764 refused for ask_user). Now the validation runs in a worker
thread, its patterns under the ``regex`` engine with one time budget, and a spent budget fails the node with its usual code.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from primer.graph.base import _map_toolcall_result, _render_end_output, _render_fanin_output
from primer.model.chat import ToolResultPart
from primer.model.graph import GraphContext, NodeOutput, _EndNode, _FanInNode

HOSTILE_SCHEMA = {"type": "object", "properties": {"s": {"type": "string", "pattern": r"(a|aa)+$"}}}
HOSTILE_VALUE = "a" * 36 + "!"  # about 5 s under stdlib re; far longer under the regex engine without a bound


def _ctx(value: str) -> GraphContext:
    return GraphContext(
        initial_input=[], iteration=0,
        nodes={"x": NodeOutput(text=value, parsed={"s": value}, history=[], iteration=0)},
    )


async def _watch(body):
    gaps = [0.0]
    state = {"stop": False}

    async def ticker() -> None:
        last = time.perf_counter()
        while not state["stop"]:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.02)
    started = time.perf_counter()
    try:
        result = await body()
    finally:
        elapsed = time.perf_counter() - started
        state["stop"] = True
        await task
    return max(gaps), result, elapsed


def _end() -> _EndNode:
    return _EndNode(id="end", output_template='{"s": "{{ nodes.x.parsed.s }}"}', output_schema=HOSTILE_SCHEMA)


def _fanin() -> _FanInNode:
    return _FanInNode(id="join", aggregate_template='{"s": "{{ nodes.x.parsed.s }}"}', output_schema=HOSTILE_SCHEMA)


CASES = {
    "end": (lambda value: _render_end_output(_end(), _ctx(value)), "end_output_invalid"),
    "fanin": (lambda value: _render_fanin_output(_fanin(), _ctx(value)), "end_output_invalid"),
    "toolcall": (
        lambda value: _map_toolcall_result(
            ToolResultPart(id="tc-1", output=json.dumps({"s": value})), output_schema=HOSTILE_SCHEMA,
        ),
        "tool_output_invalid",
    ),
}


@pytest.mark.parametrize("node", sorted(CASES))
async def test_a_catastrophic_schema_pattern_fails_the_node_without_stalling_the_loop(node):
    run, code = CASES[node]
    gap, result, elapsed = await _watch(lambda: run(HOSTILE_VALUE))
    assert result.error_code == code
    assert "output_schema" in result.error_message and "budget" in result.error_message
    assert elapsed < 2.5
    assert gap < 0.2, gap


@pytest.mark.parametrize("node", sorted(CASES))
async def test_ordinary_schema_patterns_answer_as_before(node):
    run, code = CASES[node]
    ok = await run("aaaa")
    assert ok.error_code is None
    assert ok.parsed == {"s": "aaaa"}
    bad = await run("aab")
    assert bad.error_code == code
    assert bad.error_message == "'aab' does not match '(a|aa)+$'"


async def test_a_schema_pattern_over_the_cap_fails_the_node_with_a_clear_message():
    schema = {"type": "object", "properties": {"s": {"type": "string", "pattern": "x" * 1001}}}
    result = await _map_toolcall_result(ToolResultPart(id="tc-1", output='{"s": "x"}'), output_schema=schema)
    assert result.error_code == "tool_output_invalid"
    assert "the limit is 1000" in result.error_message
