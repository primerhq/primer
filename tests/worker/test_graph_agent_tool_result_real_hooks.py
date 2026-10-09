"""``graph_agent_tool_result`` through the REAL resume-hook registry, with nothing mocked on the path.

Every registered resume hook takes ``(yield_metadata, event_payload, ctx)`` and the registry refuses a two-argument hook at
registration (tests/worker/test_resume_context.py). The graph agent-node path called the hook with TWO arguments, so the
TypeError was swallowed into a ``"resume failed"`` result and every plain graph agent-node ask_user answer or external-tool
reply was lost. The older graph tests stub ``_graph_agent_tool_result`` out (tests/worker/test_resume_graph_tool_wait.py), which
is why nothing noticed. These tests call the real function with the real ``ask_user`` and ``_external`` hooks.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import primer.agent.external_tools  # noqa: F401  (registers the "_external" hook, as a worker that never parked does)
import primer.toolset.system  # noqa: F401  (registers the "ask_user" hook)
from primer.model.chat import ToolCallResult
from primer.model.yield_ import YieldCancelled, YieldTimeout
from primer.worker import graph_resume_coordinator
from primer.worker.pool import WorkerPool
from primer.worker.yield_resume_registry import ResumeContext, register_resume_hook

from datetime import datetime, timezone


def _checkpoint(tool_name: str, tcid: str = "tc1", meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"pending_agent_yields": [{
        "tool_call_id": tcid, "tool_name": tool_name, "event_key": f"{tool_name}:s:{tcid}", "resume_metadata": meta or {},
    }]}


class _Registry:
    async def get_toolset(self, toolset_id: str):  # pragma: no cover - never awaited here
        return toolset_id


def _pool(registry: Any = None) -> SimpleNamespace:
    return SimpleNamespace(_provider_registry=registry)


async def _result(tool_name: str, payload: Any, *, pool: Any = None, session_id: str = "sess-0"):
    message = await graph_resume_coordinator.graph_agent_tool_result(
        pool if pool is not None else _pool(), _checkpoint(tool_name), "tc1", payload, session_id=session_id,
    )
    assert message is not None
    (part,) = message.parts
    return part


@pytest.mark.asyncio
async def test_an_ask_user_answer_reaches_the_node():
    part = await _result("ask_user", {"response": "blue"})
    assert part.id == "tc1"
    assert part.error is False
    assert json.loads(part.output) == {"response": "blue"}, f"the reply was lost: {part.output!r}"


@pytest.mark.asyncio
async def test_an_ask_user_timeout_reaches_the_node():
    part = await _result("ask_user", YieldTimeout(elapsed_seconds=3.0))
    assert json.loads(part.output)["timed_out"] is True, part.output


@pytest.mark.asyncio
async def test_an_ask_user_cancel_reaches_the_node():
    part = await _result(
        "ask_user", YieldCancelled(reason="skipped", cancelled_at=datetime.now(timezone.utc), elapsed_seconds=1.0),
    )
    assert json.loads(part.output)["cancelled"] is True, part.output


@pytest.mark.asyncio
async def test_an_external_call_timeout_reaches_the_node():
    part = await _result("_external", YieldTimeout(elapsed_seconds=5.0))
    assert part.error is True
    assert json.loads(part.output) == {"timed_out": True}, f"the timeout was lost: {part.output!r}"


@pytest.mark.asyncio
async def test_an_external_call_result_reaches_the_node():
    part = await _result("_external", {"result": "42", "is_error": False})
    assert part.error is False
    assert part.output == "42", part.output


@pytest.mark.asyncio
async def test_the_hook_gets_the_context_the_agent_session_path_builds():
    seen: list[ResumeContext] = []

    def hook(meta, payload, ctx: ResumeContext) -> ToolCallResult:
        seen.append(ctx)
        return ToolCallResult(output="ok", is_error=False)

    register_resume_hook("test_graph_ctx_tool", hook)
    registry = _Registry()
    part = await _result("test_graph_ctx_tool", {}, pool=_pool(registry), session_id="sess-1")

    assert part.output == "ok"
    (ctx,) = seen
    assert (ctx.tool_name, ctx.tool_call_id, ctx.session_id) == ("test_graph_ctx_tool", "tc1", "sess-1")
    assert ctx.resolve_provider == registry.get_toolset, "a python toolset's hook could not reach its provider"


@pytest.mark.asyncio
async def test_without_a_provider_registry_the_context_says_so():
    seen: list[ResumeContext] = []

    def hook(meta, payload, ctx: ResumeContext) -> ToolCallResult:
        seen.append(ctx)
        return ToolCallResult(output="ok", is_error=False)

    register_resume_hook("test_graph_ctx_tool_no_registry", hook)
    await _result("test_graph_ctx_tool_no_registry", {}, pool=_pool(None), session_id="sess-4")
    assert seen[0].resolve_provider is None and seen[0].session_id == "sess-4"


@pytest.mark.asyncio
async def test_the_pool_method_forwards_the_session_id():
    """``WorkerPool._graph_agent_tool_result`` is the seam both callers use; run its real body against a stand-in ``self``."""
    seen: list[ResumeContext] = []

    def hook(meta, payload, ctx: ResumeContext) -> ToolCallResult:
        seen.append(ctx)
        return ToolCallResult(output="ok", is_error=False)

    register_resume_hook("test_graph_ctx_tool_pool", hook)
    message = await WorkerPool._graph_agent_tool_result(
        _pool(), _checkpoint("test_graph_ctx_tool_pool"), "tc1", {}, session_id="sess-2",
    )
    assert message is not None and seen[0].session_id == "sess-2"


@pytest.mark.asyncio
async def test_the_session_id_is_required():
    """Every caller holds the session being resumed, so the hook's ``ctx.session_id`` can never silently be None."""
    with pytest.raises(TypeError, match="session_id"):
        await graph_resume_coordinator.graph_agent_tool_result(_pool(), _checkpoint("ask_user"), "tc1", {})
    with pytest.raises(TypeError, match="session_id"):
        await WorkerPool._graph_agent_tool_result(_pool(), _checkpoint("ask_user"), "tc1", {})


@pytest.mark.asyncio
async def test_the_invocation_services_closure_forwards_the_session_id():
    """The GraphFrame leaf path resolves an ask_user answer through ``InvocationServices.graph_agent_tool_result``."""
    from primer.worker.session_resume_coordinator import build_invocation_services

    seen: list[ResumeContext] = []

    def hook(meta, payload, ctx: ResumeContext) -> ToolCallResult:
        seen.append(ctx)
        return ToolCallResult(output="ok", is_error=False)

    register_resume_hook("test_graph_ctx_tool_services", hook)

    class _StandIn(SimpleNamespace):
        async def _graph_agent_tool_result(self, checkpoint, tcid, payload, *, session_id, event_key=None):
            return await WorkerPool._graph_agent_tool_result(self, checkpoint, tcid, payload, session_id=session_id, event_key=event_key)

    pool = _StandIn(_provider_registry=None, _storage=None, _approval_resolver=None)
    services = build_invocation_services(pool, SimpleNamespace(id="sess-3"), None, None, SimpleNamespace())
    message = await services.graph_agent_tool_result(_checkpoint("test_graph_ctx_tool_services"), "tc1", {})
    assert message is not None and seen[0].session_id == "sess-3"
