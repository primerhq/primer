"""Integration tests for the InternalToolsetProvider yield protocol.

Verifies:
* legacy handlers (no ctx) still work,
* yielding handlers receive the injected ToolContext,
* Yielded sentinels are stamped with the registered tool name,
* the provider raises YieldToWorker on yield,
* the sleep tool's full migration (handler yields, resume hook
  registered).
"""

from __future__ import annotations

import asyncio

import pytest

from primer.model.chat import Tool, ToolCallResult
from primer.model.except_ import ConfigError
from primer.model.yield_ import (
    ToolContext,
    YieldCancelled,
    YieldToWorker,
    Yielded,
)
from primer.toolset.internal import InternalToolsetProvider, _handler_takes_ctx
from primer.worker.yield_resume_registry import (
    _reset_for_tests,
    get_resume_hook,
    has_resume_hook,
    register_resume_hook,
)


# ===========================================================================
# Helpers
# ===========================================================================


def _make_tool(toolset_id: str, name: str) -> Tool:
    return Tool(
        id=name,
        description=f"test tool {name}",
        toolset_id=toolset_id,
        args_schema={"type": "object"},
    )


# ===========================================================================
# _handler_takes_ctx introspection
# ===========================================================================


class TestHandlerSignatureIntrospection:
    def test_legacy_handler_no_ctx(self):
        async def handler(arguments):
            return ToolCallResult(output="ok")
        assert _handler_takes_ctx(handler) is False

    def test_yielding_handler_keyword_only_ctx(self):
        async def handler(arguments, *, ctx: ToolContext):  # noqa: ARG001
            return Yielded(tool_name="", event_key="x:1")
        assert _handler_takes_ctx(handler) is True

    def test_yielding_handler_positional_or_keyword_ctx(self):
        async def handler(arguments, ctx=None):  # noqa: ARG001
            return ToolCallResult(output="ok")
        assert _handler_takes_ctx(handler) is True


# ===========================================================================
# Dispatch — legacy handler unchanged
# ===========================================================================


@pytest.mark.asyncio
class TestLegacyDispatch:
    async def test_legacy_handler_called_without_ctx(self):
        async def handler(arguments):
            return ToolCallResult(output=f"got {arguments['x']}")
        tool = _make_tool("ts", "echo")
        provider = InternalToolsetProvider(
            toolset_id="ts", registry={"echo": (tool, handler)},
        )
        result = await provider.call(tool_name="echo", arguments={"x": 42})
        assert result.output == "got 42"

    async def test_legacy_handler_with_ctx_supplied_ignores_it(self):
        # If a legacy handler doesn't declare ctx, the provider must
        # NOT pass it — passing unexpected kwargs would TypeError.
        async def handler(arguments):
            return ToolCallResult(output="ok")
        tool = _make_tool("ts", "echo")
        provider = InternalToolsetProvider(
            toolset_id="ts", registry={"echo": (tool, handler)},
        )
        result = await provider.call(
            tool_name="echo",
            arguments={},
            ctx=ToolContext(tool_call_id="tc-1", session_id="s", workspace_id="w"),
        )
        assert result.output == "ok"


# ===========================================================================
# Dispatch — yielding handler
# ===========================================================================


@pytest.mark.asyncio
class TestYieldingDispatch:
    async def test_yielding_handler_receives_ctx_and_yields(self):
        captured: list[ToolContext] = []

        async def handler(arguments, *, ctx: ToolContext):
            captured.append(ctx)
            return Yielded(
                tool_name="",  # provider stamps this
                event_key=f"timer:{ctx.tool_call_id}",
                timeout=arguments.get("seconds"),
            )

        tool = _make_tool("ts", "snooze")
        provider = InternalToolsetProvider(
            toolset_id="ts", registry={"snooze": (tool, handler)},
        )
        ctx = ToolContext(tool_call_id="tc-1", session_id="s", workspace_id="w")
        with pytest.raises(YieldToWorker) as info:
            await provider.call(
                tool_name="snooze",
                arguments={"seconds": 5.0},
                ctx=ctx,
            )
        assert info.value.tool_call_id == "tc-1"
        assert info.value.yielded.event_key == "timer:tc-1"
        # Provider stamps the registered tool name onto the Yielded.
        assert info.value.yielded.tool_name == "snooze"
        # Handler saw the same context the caller passed.
        assert captured == [ctx]

    async def test_yield_without_ctx_raises_config_error(self):
        # A handler that declares ctx but is called WITHOUT one and
        # returns Yielded → ConfigError (programming bug).
        async def handler(arguments, *, ctx: ToolContext = None):  # type: ignore[assignment]
            return Yielded(tool_name="", event_key="x:1")
        tool = _make_tool("ts", "broken")
        provider = InternalToolsetProvider(
            toolset_id="ts", registry={"broken": (tool, handler)},
        )
        # Calling without ctx means the handler runs with ctx=None;
        # returning Yielded with ctx=None is the bug we trap.
        with pytest.raises(ConfigError, match="ToolContext"):
            await provider.call(tool_name="broken", arguments={})


# ===========================================================================
# Resume hook registry
# ===========================================================================


class TestResumeHookRegistry:
    def setup_method(self):
        # Snapshot + restore the registry around each test so we
        # don't leak state.
        from primer.worker.yield_resume_registry import _registry
        self._snapshot = dict(_registry)
        _reset_for_tests()

    def teardown_method(self):
        from primer.worker.yield_resume_registry import _registry
        _reset_for_tests()
        _registry.update(self._snapshot)

    def test_register_then_lookup(self):
        def hook(meta, payload):
            return ToolCallResult(output="ok")
        register_resume_hook("widget", hook)
        assert has_resume_hook("widget")
        assert get_resume_hook("widget") is hook

    def test_register_same_hook_twice_is_idempotent(self):
        def hook(meta, payload):
            return ToolCallResult(output="ok")
        register_resume_hook("widget", hook)
        register_resume_hook("widget", hook)  # no raise
        assert get_resume_hook("widget") is hook

    def test_register_different_hook_for_same_name_raises(self):
        def hook_a(meta, payload):
            return ToolCallResult(output="a")

        def hook_b(meta, payload):
            return ToolCallResult(output="b")
        register_resume_hook("widget", hook_a)
        with pytest.raises(ConfigError, match="already registered"):
            register_resume_hook("widget", hook_b)

    def test_lookup_unknown_raises_loudly(self):
        with pytest.raises(ConfigError, match="no resume hook"):
            get_resume_hook("never-registered")

    def test_has_returns_false_for_unknown(self):
        assert has_resume_hook("never-registered") is False


# ===========================================================================
# End-to-end sleep tool — yield + resume
# ===========================================================================


@pytest.mark.asyncio
class TestSleepToolE2E:
    async def test_zero_seconds_short_circuits(self):
        # Sleep with seconds=0 returns directly without yielding —
        # there's nothing to wait for.
        from primer.toolset.misc import build_misc_toolset
        provider = build_misc_toolset()
        result = await provider.call(
            tool_name="sleep",
            arguments={"seconds": 0.0},
            ctx=ToolContext(tool_call_id="tc-1", session_id="s", workspace_id="w"),
        )
        assert result.is_error is False
        import json
        body = json.loads(result.output)
        assert body == {"requested_seconds": 0.0, "elapsed_seconds": 0.0}

    async def test_nonzero_seconds_yields(self):
        from primer.toolset.misc import build_misc_toolset
        provider = build_misc_toolset()
        ctx = ToolContext(tool_call_id="tc-z", session_id="s", workspace_id="w")
        with pytest.raises(YieldToWorker) as info:
            await provider.call(
                tool_name="sleep", arguments={"seconds": 30.0}, ctx=ctx,
            )
        assert info.value.yielded.tool_name == "sleep"
        assert info.value.yielded.event_key == "timer:tc-z"
        assert info.value.yielded.timeout == 30.0
        assert info.value.yielded.resume_metadata == {"requested_seconds": 30.0}

    async def test_sleep_resume_hook_normal_event(self):
        # Look up the registered hook and exercise the resume path
        # with a normal (empty) event payload — should return the
        # standard {requested_seconds, elapsed_seconds} shape.
        from datetime import datetime, timedelta, timezone
        hook = get_resume_hook("sleep")
        parked_at = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
        meta = {
            "requested_seconds": 30.0,
            "parked_at_iso": parked_at.isoformat(),
        }
        result = hook(meta, {})  # empty event payload (timer fire)
        assert result.is_error is False
        import json
        body = json.loads(result.output)
        assert body["requested_seconds"] == 30.0
        # elapsed_seconds is computed from real now() so it'll be
        # large; just assert it's positive (we parked seconds-to-
        # decades ago for the test fixture).
        assert body["elapsed_seconds"] > 0

    async def test_sleep_resume_hook_cancelled(self):
        from datetime import datetime, timezone
        hook = get_resume_hook("sleep")
        parked_at = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
        meta = {
            "requested_seconds": 30.0,
            "parked_at_iso": parked_at.isoformat(),
        }
        cancelled = YieldCancelled(
            reason="user changed mind",
            cancelled_at=datetime.now(timezone.utc),
            elapsed_seconds=5.0,
        )
        result = hook(meta, cancelled)
        assert result.is_error is False
        import json
        body = json.loads(result.output)
        assert body["cancelled"] is True
        assert body["cancel_reason"] == "user changed mind"
        assert body["requested_seconds"] == 30.0

    async def test_sleep_negative_seconds_validation_error(self):
        # Sleep's pydantic schema enforces ge=0.0; the existing
        # 300s cap was removed (it's now the global cap), so very
        # large values are accepted at the validation layer.
        from primer.toolset.misc import build_misc_toolset
        provider = build_misc_toolset()
        ctx = ToolContext(tool_call_id="tc-x", session_id="s", workspace_id="w")
        result = await provider.call(
            tool_name="sleep",
            arguments={"seconds": -1.0},
            ctx=ctx,
        )
        assert result.is_error is True

    async def test_sleep_large_seconds_yields_without_internal_cap(self):
        # Removal of the 300s cap means a 1-hour sleep yields cleanly
        # at the tool layer; the worker pool's global cap (M2)
        # bounds total wait.
        from primer.toolset.misc import build_misc_toolset
        provider = build_misc_toolset()
        ctx = ToolContext(tool_call_id="tc-l", session_id="s", workspace_id="w")
        with pytest.raises(YieldToWorker) as info:
            await provider.call(
                tool_name="sleep", arguments={"seconds": 3600.0}, ctx=ctx,
            )
        assert info.value.yielded.timeout == 3600.0
