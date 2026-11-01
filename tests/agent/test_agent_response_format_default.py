"""AG-M3, ruling D14: an agent's own ``response_format`` is the default for its runs.

``Agent.response_format`` says the runtime forwards it as the agent's ``invoke(response_format=...)``; nothing read it. The
agent executor now uses it when the caller passes none (a schema the caller passes still wins), and a delegated run
(``run_subagent``, and its resume) is sent it. An agent without one sends none, as before.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Message, TextPart, ToolResultPart
from primer.model.principal import PrincipalRef
from primer.model.yield_ import YieldToWorker
from tests.agent.test_subagent_resume import (
    _final_text_script,
    _provider_row,
    _ProviderRegistry,
    _RecordingScriptedLLM,
    _StorageProvider,
    _tool_call_script,
    _YieldingToolsetProvider,
)
from tests.agent.test_workspace_executor import _build_session, _drain, _model

_AGENT_FORMAT = {"type": "object", "title": "agent"}
_CALLER_FORMAT = {"type": "object", "title": "caller"}


class _FormatRecordingLLM(_RecordingScriptedLLM):
    def __init__(self, *, scripts):
        super().__init__(scripts=scripts)
        self.formats: list = []

    def stream(self, *, model, messages, **kwargs):  # noqa: ANN001
        self.formats.append(kwargs.get("response_format"))
        return super().stream(model=model, messages=messages, **kwargs)

    async def list_models(self):
        return ["m"]


def _agent(response_format=None, tools=()) -> Agent:
    return Agent(
        id="agent-sub", description="x", model=AgentModel(profile_id="prov-1--m1"), system_prompt=["you are a subagent"],
        tools=list(tools), response_format=response_format,
    )


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")


@needs_git
@pytest.mark.asyncio
@pytest.mark.parametrize("agent_format,passed,sent", [
    (_AGENT_FORMAT, None, _AGENT_FORMAT),
    (_AGENT_FORMAT, _CALLER_FORMAT, _CALLER_FORMAT),
    (None, None, None),
    (None, _CALLER_FORMAT, _CALLER_FORMAT),
], ids=["agent-default", "caller-wins", "none", "caller-only"])
async def test_the_executor_falls_back_to_its_agents_format(tmp_path: Path, agent_format, passed, sent):
    backend, _, session = await _build_session(tmp_path)
    try:
        llm = _FormatRecordingLLM(scripts=[_final_text_script("{}")])
        executor = WorkspaceAgentExecutor(
            agent=_agent(agent_format), llm=llm,  # type: ignore[arg-type]
            llm_model=_model(), tool_manager=ToolExecutionManager.for_workspace(toolset_providers={}, session=session),
            session=session,
        )
        await _drain(executor.invoke([Message(role="user", parts=[TextPart(text="hi")])], response_format=passed))
        assert llm.formats == [sent]
    finally:
        await session.aclose()
        await backend.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_format", [_AGENT_FORMAT, None], ids=["agent-format", "none"])
async def test_a_delegated_run_and_its_resume_are_sent_the_agents_format(agent_format):
    from primer.agent.invoke import invocation_depth_guard, resume_subagent, run_subagent

    llm = _FormatRecordingLLM(scripts=[_tool_call_script("t1__wait", "call-1"), _final_text_script("{}")])
    agent = _agent(agent_format, tools=["t1__wait"])
    storage = _StorageProvider(agent=agent, provider_row=_provider_row())
    registry = _ProviderRegistry(llm=llm, toolset=_YieldingToolsetProvider())
    admin = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")
    with pytest.raises(YieldToWorker) as parked:
        with invocation_depth_guard():
            await run_subagent(
                agent_id="agent-sub", prompt="task", storage_provider=storage, provider_registry=registry,
                session_id="s", workspace_id="w", invoke_tool_call_id="parent", identity=admin,
            )
    (frame,) = parked.value.frames
    await resume_subagent(
        agent_id=frame.agent_id, context=frame.context, llm_messages=frame.llm_messages,
        child_result=ToolResultPart(id="call-1", output="r1"), depth=frame.depth, storage_provider=storage,
        provider_registry=registry, invoke_tool_call_id=frame.tool_call_id,
    )
    assert llm.formats == [agent_format, agent_format]
