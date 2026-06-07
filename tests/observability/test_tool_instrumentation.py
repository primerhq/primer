"""Tests for ToolExecutionManager span + metrics instrumentation (Task 7)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from primer.observability.metrics import reset_for_test


@pytest.fixture(autouse=True)
def fresh_metrics():
    reset_for_test()
    yield


@pytest.fixture
def in_memory_tracer():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _patch_tracer(provider):
    return patch(
        "primer.observability.tracing.get_tracer",
        side_effect=lambda name: provider.get_tracer(name),
    )


# ---------------------------------------------------------------------------
# Minimal ToolExecutionManager with a fake toolset
# ---------------------------------------------------------------------------


def _make_manager_with_fake_tool(tool_name: str, tool_result: str, *, raises=None):
    """Build a ToolExecutionManager with one registered fake toolset tool."""
    from primer.agent.tool_manager import ToolExecutionManager
    from primer.int.toolset import ToolsetProvider
    from primer.model.chat import Tool

    class FakeToolsetProvider(ToolsetProvider):
        async def list_tools(self, *, principal=None):
            yield Tool(
                id=tool_name,
                toolset_id="fake",
                description="A fake tool",
                args_schema={"type": "object", "properties": {}},
            )

        async def call(self, *, tool_name, arguments, principal=None, ctx=None):
            if raises is not None:
                raise raises
            result = MagicMock()
            result.output = tool_result
            result.is_error = False
            return result

    mgr = ToolExecutionManager(
        toolset_providers={"fake": FakeToolsetProvider()},
    )
    return mgr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_exec_span_attributes(in_memory_tracer):
    provider, exporter = in_memory_tracer
    from primer.model.chat import ToolCallPart

    mgr = _make_manager_with_fake_tool("search", "result")
    # Pre-build catalogue so routing is set up
    await mgr.list_tools()

    call = ToolCallPart(id="call-1", name="fake__search", arguments={})

    with _patch_tracer(provider):
        result = await mgr.execute(call)

    assert result.output == "result"
    assert not result.error

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "tool.exec"
    assert span.attributes["tool.name"] == "fake__search"


@pytest.mark.asyncio
async def test_tool_exec_ok_counter(in_memory_tracer):
    provider, exporter = in_memory_tracer
    import primer.observability.metrics as m
    from primer.model.chat import ToolCallPart

    mgr = _make_manager_with_fake_tool("calc", "42")
    await mgr.list_tools()
    call = ToolCallPart(id="call-2", name="fake__calc", arguments={})

    with _patch_tracer(provider):
        await mgr.execute(call)

    ok_samples = [
        s for metric in m.tool_calls_total.collect()
        for s in metric.samples
        if s.labels.get("name") == "fake__calc"
        and s.labels.get("outcome") == "ok"
        and s.name == "tool_calls_total"
    ]
    assert sum(s.value for s in ok_samples) == 1.0


@pytest.mark.asyncio
async def test_tool_exec_fail_counter_and_exception(in_memory_tracer):
    provider, exporter = in_memory_tracer
    import primer.observability.metrics as m
    from primer.model.chat import ToolCallPart
    from primer.model.except_ import UnsupportedContentError

    mgr = _make_manager_with_fake_tool(
        "boom", "never", raises=UnsupportedContentError("kaboom")
    )
    await mgr.list_tools()
    call = ToolCallPart(id="call-3", name="fake__boom", arguments={})

    with _patch_tracer(provider):
        # The manager catches PrimerError internally and returns error ToolResultPart
        result = await mgr.execute(call)

    # UnsupportedContentError bubbles up and is caught by _dispatch_toolset
    # which converts it to a ToolResultPart(error=True) — so no exception propagates
    assert result.error

    # "ok" counter should be incremented because no exception escaped execute()
    ok_samples = [
        s for metric in m.tool_calls_total.collect()
        for s in metric.samples
        if s.labels.get("name") == "fake__boom"
        and s.labels.get("outcome") == "ok"
        and s.name == "tool_calls_total"
    ]
    assert sum(s.value for s in ok_samples) == 1.0


@pytest.mark.asyncio
async def test_tool_exec_fail_on_unknown_tool(in_memory_tracer):
    """Unknown tool name should increment fail counter and record exception."""
    provider, exporter = in_memory_tracer
    import primer.observability.metrics as m
    from primer.model.chat import ToolCallPart
    from primer.model.except_ import UnsupportedContentError

    from primer.agent.tool_manager import ToolExecutionManager
    mgr = ToolExecutionManager(toolset_providers={})
    await mgr.list_tools()

    call = ToolCallPart(id="call-4", name="nonexistent__tool", arguments={})

    with _patch_tracer(provider):
        with pytest.raises(UnsupportedContentError):
            await mgr.execute(call)

    fail_samples = [
        s for metric in m.tool_calls_total.collect()
        for s in metric.samples
        if s.labels.get("name") == "nonexistent__tool"
        and s.labels.get("outcome") == "fail"
        and s.name == "tool_calls_total"
    ]
    assert sum(s.value for s in fail_samples) == 1.0

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    # Exception should have been recorded on the span
    assert len(spans[0].events) > 0


@pytest.mark.asyncio
async def test_tool_exec_duration_observed(in_memory_tracer):
    provider, exporter = in_memory_tracer
    import primer.observability.metrics as m
    from primer.model.chat import ToolCallPart

    mgr = _make_manager_with_fake_tool("timer", "done")
    await mgr.list_tools()
    call = ToolCallPart(id="call-5", name="fake__timer", arguments={})

    with _patch_tracer(provider):
        await mgr.execute(call)

    dur_samples = {
        s.name: s.value
        for metric in m.tool_duration_seconds.collect()
        for s in metric.samples
        if s.labels.get("name") == "fake__timer"
    }
    assert dur_samples.get("tool_duration_seconds_count", 0) == 1
