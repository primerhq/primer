"""Tests for matrix.agent.workspace_executor.WorkspaceAgentExecutor."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
)
from primer.model.except_ import ConflictError
from primer.model.provider import LLMModel
from primer.model.workspace_session import (
    AgentBinding,
    SessionStatus,
)
from primer.model.workspace import (
    FileMount,
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
)
from primer.workspace import (
    LocalWorkspaceBackend,
    WorkspaceBackendFactory,
)


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (StateRepo needs it)",
)


# ===========================================================================
# Fakes
# ===========================================================================


class _FakeLLM:
    """Stub :class:`LLM` returning scripted streams turn-by-turn."""

    def __init__(self, *, scripts: list[list[StreamEvent]]) -> None:
        self._scripts = scripts
        self._cursor = 0
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs) -> AsyncIterator[StreamEvent]:
        self.calls.append({"model": model, "messages": list(messages), **kwargs})
        idx = min(self._cursor, len(self._scripts) - 1)
        self._cursor += 1
        return self._stream_impl(self._scripts[idx])

    async def _stream_impl(
        self, events: list[StreamEvent]
    ) -> AsyncIterator[StreamEvent]:
        for ev in events:
            yield ev


# ===========================================================================
# Helpers
# ===========================================================================


def _agent(*, system_prompt=None) -> Agent:
    return Agent(
        id="researcher",
        description="Research agent",
        model=AgentModel(provider_id="openai-1", model_name="m"),
        system_prompt=list(system_prompt or []),
    )


def _model() -> LLMModel:
    return LLMModel(name="m", context_length=128_000)


def _template() -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="research",
        description="research workstation",
        provider_id="local-1",
        files=[
            FileMount(
                path="src/main.py",
                source={"kind": "inline", "content": "def main(): return 'TODO'\n"},
            ),
        ],
    )


async def _build_session(tmp_path: Path):
    config_entry = WorkspaceProvider(
        id="local-1",
        provider=WorkspaceProviderType.LOCAL,
        config=LocalWorkspaceConfig(path=str(tmp_path / "wsroot")),
    )
    backend = WorkspaceBackendFactory.create(config_entry)
    assert isinstance(backend, LocalWorkspaceBackend)
    await backend.initialize()
    workspace = await backend.create(_template())
    binding = AgentBinding(
        agent_id="researcher",
        agent_name="Research Agent",
        registered_tool_ids=[],
    )
    session = await workspace.start_session(binding, instructions="hello")
    return backend, workspace, session


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


# ===========================================================================
# Construction & system prompt composition
# ===========================================================================


class TestConstruction:
    @pytest.mark.asyncio
    async def test_system_prompt_composed_with_workspace_fragment(
        self, tmp_path: Path
    ) -> None:
        backend, _, session = await _build_session(tmp_path)
        try:
            llm = _FakeLLM(
                scripts=[
                    [
                        TextDelta(text="ok", index=0),
                        Done(stop_reason="stop", raw_reason="stop"),
                    ]
                ]
            )
            mgr = ToolExecutionManager.for_workspace(
                toolset_providers={},
                session=session,
            )
            executor = WorkspaceAgentExecutor(
                agent=_agent(system_prompt=["base"]),
                llm=llm,  # type: ignore[arg-type]
                llm_model=_model(),
                tool_manager=mgr,
                session=session,
            )
            await _drain(
                executor.invoke([Message(role="user", parts=[TextPart(text="hi")])])
            )
            sys_msg = llm.calls[0]["messages"][0]
            assert sys_msg.role == "system"
            sys_text = sys_msg.parts[0].text
            assert "base" in sys_text
            assert session.session_id in sys_text  # workspace fragment merged
        finally:
            await session.aclose()
            await backend.aclose()


# ===========================================================================
# Persistence via commit_state
# ===========================================================================


class TestPersistence:
    @pytest.mark.asyncio
    async def test_turn_persists_to_messages_jsonl(self, tmp_path: Path) -> None:
        backend, workspace, session = await _build_session(tmp_path)
        try:
            llm = _FakeLLM(
                scripts=[
                    [
                        TextDelta(text="hello!", index=0),
                        Done(stop_reason="stop", raw_reason="stop"),
                    ]
                ]
            )
            mgr = ToolExecutionManager.for_workspace(
                toolset_providers={}, session=session
            )
            executor = WorkspaceAgentExecutor(
                agent=_agent(),
                llm=llm,  # type: ignore[arg-type]
                llm_model=_model(),
                tool_manager=mgr,
                session=session,
            )
            await _drain(
                executor.invoke([Message(role="user", parts=[TextPart(text="hi")])])
            )
            jsonl_path = (
                workspace.root
                / workspace.template.state_path
                / "sessions"
                / session.session_id
                / "messages.jsonl"
            )
            content = jsonl_path.read_text(encoding="utf-8")
            lines = [ln for ln in content.splitlines() if ln.strip()]
            # Initial instruction "hello" + new user msg + assistant msg.
            assert len(lines) >= 3
            assert "hello!" in lines[-1]  # assistant text in last line
        finally:
            await session.aclose()
            await backend.aclose()


# ===========================================================================
# Status transitions
# ===========================================================================


class TestStatusTransitions:
    @pytest.mark.asyncio
    async def test_invoke_on_ended_session_raises(self, tmp_path: Path) -> None:
        backend, _, session = await _build_session(tmp_path)
        try:
            await session.aclose()  # ENDED
            llm = _FakeLLM(scripts=[[]])
            mgr = ToolExecutionManager.for_workspace(
                toolset_providers={}, session=session
            )
            executor = WorkspaceAgentExecutor(
                agent=_agent(),
                llm=llm,  # type: ignore[arg-type]
                llm_model=_model(),
                tool_manager=mgr,
                session=session,
            )
            with pytest.raises(ConflictError):
                await _drain(
                    executor.invoke(
                        [Message(role="user", parts=[TextPart(text="hi")])]
                    )
                )
        finally:
            await backend.aclose()

    @pytest.mark.asyncio
    async def test_question_mark_transitions_to_waiting(
        self, tmp_path: Path
    ) -> None:
        backend, _, session = await _build_session(tmp_path)
        try:
            llm = _FakeLLM(
                scripts=[
                    [
                        TextDelta(text="What's your scope?", index=0),
                        Done(stop_reason="stop", raw_reason="stop"),
                    ]
                ]
            )
            mgr = ToolExecutionManager.for_workspace(
                toolset_providers={}, session=session
            )
            executor = WorkspaceAgentExecutor(
                agent=_agent(),
                llm=llm,  # type: ignore[arg-type]
                llm_model=_model(),
                tool_manager=mgr,
                session=session,
            )
            await _drain(
                executor.invoke([Message(role="user", parts=[TextPart(text="hi")])])
            )
            assert await session.status() == SessionStatus.WAITING
            ws = await session.waiting_state()
            assert ws is not None
            assert ws.kind == "user_input"
            assert "scope" in ws.prompt
        finally:
            await session.aclose()
            await backend.aclose()

    @pytest.mark.asyncio
    async def test_pause_request_transitions_to_paused(self, tmp_path: Path) -> None:
        backend, _, session = await _build_session(tmp_path)
        try:
            await session.request_pause()
            llm = _FakeLLM(scripts=[[]])
            mgr = ToolExecutionManager.for_workspace(
                toolset_providers={}, session=session
            )
            executor = WorkspaceAgentExecutor(
                agent=_agent(),
                llm=llm,  # type: ignore[arg-type]
                llm_model=_model(),
                tool_manager=mgr,
                session=session,
            )
            await _drain(
                executor.invoke([Message(role="user", parts=[TextPart(text="hi")])])
            )
            assert await session.status() == SessionStatus.PAUSED
            assert llm.calls == []
        finally:
            await session.aclose()
            await backend.aclose()
