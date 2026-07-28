"""A resume hook learns which park it is answering.

Hooks for tools that park under one fixed name never needed this. A python
toolset's tool names are operator-chosen and its source lives in a record, so
its hook cannot know whose code to run from yield_metadata alone.
"""

from __future__ import annotations

import pytest

from primer.model.chat import ToolCallResult
from primer.worker.yield_resume_registry import (
    ResumeContext,
    get_resume_hook,
    register_resume_hook,
)


def test_the_hook_receives_the_context() -> None:
    seen: dict[str, str] = {}

    def hook(meta, payload, ctx: ResumeContext) -> ToolCallResult:
        seen["tool"] = ctx.tool_name
        seen["tcid"] = ctx.tool_call_id
        return ToolCallResult(output="ok", is_error=False)

    register_resume_hook("test_ctx_tool", hook)
    get_resume_hook("test_ctx_tool")(
        {}, {}, ResumeContext(tool_name="test_ctx_tool", tool_call_id="tc-1")
    )
    assert seen == {"tool": "test_ctx_tool", "tcid": "tc-1"}


def test_a_two_argument_hook_is_rejected_at_registration() -> None:
    # A hook predating the context would raise at RESUME time, on a session
    # already parked for however long the operator took to answer. Catch it
    # when it registers instead.
    def old_style(meta, payload):  # pragma: no cover - never called
        return ToolCallResult(output="", is_error=False)

    with pytest.raises(TypeError) as exc:
        register_resume_hook("test_old_style", old_style)
    assert "ResumeContext" in str(exc.value)


@pytest.mark.asyncio
async def test_the_context_can_resolve_a_provider() -> None:
    sentinel = object()

    async def _resolve(_tid: str):
        return sentinel

    async def hook(meta, payload, ctx: ResumeContext) -> ToolCallResult:
        provider = await ctx.resolve_provider(meta["toolset_id"])
        return ToolCallResult(
            output="found" if provider is sentinel else "no", is_error=False
        )

    register_resume_hook("test_resolver_tool", hook)
    ctx = ResumeContext(
        tool_name="test_resolver_tool",
        tool_call_id="tc-1",
        resolve_provider=_resolve,
    )
    out = await get_resume_hook("test_resolver_tool")({"toolset_id": "ts-1"}, {}, ctx)
    assert out.output == "found"


def test_a_missing_resolver_is_visible_not_silent() -> None:
    # The graph tool_call path has no registry in scope. A hook that needs one
    # must be able to tell, rather than crash on None.
    ctx = ResumeContext(tool_name="t", tool_call_id="tc-1")
    assert ctx.resolve_provider is None


def test_the_shipped_hooks_all_accept_the_context() -> None:
    import inspect

    from primer.toolset._system_tools import ask_user_resume
    from primer.toolset.mcp import mcp_task_resume
    from primer.toolset.misc import sleep_resume
    from primer.toolset.workspaces import watch_files_resume

    for hook in (ask_user_resume, watch_files_resume, mcp_task_resume, sleep_resume):
        params = list(inspect.signature(hook).parameters)
        assert len(params) == 3, hook.__name__
        assert params[2] == "ctx", hook.__name__
