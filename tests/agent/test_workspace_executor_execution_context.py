"""WorkspaceAgentExecutor builds a workspace ExecutionContext and creates the
per-session artifact directory. Reuses the live-session fixture from
``tests.agent.test_workspace_executor`` (established cross-module pattern)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.chat import Done, Message, TextDelta, TextPart
from tests.agent.test_workspace_executor import (
    _FakeLLM,
    _agent,
    _build_session,
    _model,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (StateRepo needs it)",
)


@pytest.mark.asyncio
async def test_workspace_execution_context_and_artifact_dir(tmp_path: Path) -> None:
    _backend, _workspace, session = await _build_session(tmp_path)
    mgr = ToolExecutionManager.for_workspace(toolset_providers={}, session=session)
    ex = WorkspaceAgentExecutor(
        agent=_agent(system_prompt=["base"]),
        llm=_FakeLLM(scripts=[]),  # type: ignore[arg-type]
        llm_model=_model(),
        tool_manager=mgr,
        session=session,
    )
    ctx = ex._execution_context
    assert ctx.surface == "workspace"
    assert ctx.workspace_id == session.workspace_id
    assert ctx.session_id == session.session_id
    assert ctx.artifact_dir == f"artifacts/{session.session_id}"


@pytest.mark.asyncio
async def test_workspace_invoke_creates_artifact_dir(tmp_path: Path) -> None:
    _backend, _workspace, session = await _build_session(tmp_path)
    mgr = ToolExecutionManager.for_workspace(toolset_providers={}, session=session)
    ex = WorkspaceAgentExecutor(
        agent=_agent(system_prompt=["base"]),
        llm=_FakeLLM(
            scripts=[
                [
                    TextDelta(text="ok", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        ),  # type: ignore[arg-type]
        llm_model=_model(),
        tool_manager=mgr,
        session=session,
    )
    async for _ in ex.invoke([Message(role="user", parts=[TextPart(text="hi")])]):
        pass

    root = session.workspace_root
    assert root is not None
    assert (root / "artifacts" / session.session_id).is_dir()
