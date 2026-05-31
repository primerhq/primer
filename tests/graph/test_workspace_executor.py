"""Tests for primer.graph.workspace_executor.WorkspaceGraphExecutor.

These tests build a real :class:`StateRepo` on a tmp path so we
exercise the git-versioning behaviour end-to-end (one commit per
turn, per superstep, plus the final ENDED transition).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Message,
    StreamEvent,
    TextDelta,
    Tool,
    ToolCallEnd,
    ToolCallStart,
)
from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _GraphNodeRef,
    _StaticEdge,
)
from primer.model.provider import LLMModel
from primer.workspace.local.state import LocalStateRepo as StateRepo


# ===========================================================================
# Fakes
# ===========================================================================


class _FakeLLM:
    def __init__(self, *, scripts: list[list[StreamEvent]]) -> None:
        self._scripts = scripts
        self._cursor = 0
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(
        self,
        *,
        model: str,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append({"model": model, "messages": list(messages), **kwargs})
        idx = min(self._cursor, len(self._scripts) - 1)
        self._cursor += 1
        return self._stream_impl(self._scripts[idx])

    async def _stream_impl(
        self,
        events: list[StreamEvent],
    ) -> AsyncIterator[StreamEvent]:
        for ev in events:
            yield ev


class _FakeToolsetProvider:
    """Minimal :class:`ToolsetProvider` test double exposing one tool."""

    def __init__(self, *, tool_id: str, output: str) -> None:
        self._tool_id = tool_id
        self._output = output
        self.calls: list[dict[str, Any]] = []

    async def list_tools(self, *, principal: str | None = None):
        yield Tool(
            id=self._tool_id,
            description=f"fake tool {self._tool_id}",
            toolset_id="fake",
            args_schema={"type": "object", "properties": {}},
        )

    async def call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        principal: str | None = None,
    ):
        self.calls.append(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "principal": principal,
            }
        )
        from primer.int.toolset import ToolCallResult

        return ToolCallResult(output=self._output, is_error=False)


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(provider_id="p", model_name="m"),
    )


def _model() -> LLMModel:
    return LLMModel(name="m", context_length=128_000)


async def _make_state_repo(tmp_path: Path, workspace_id: str = "ws-test") -> StateRepo:
    repo = StateRepo(tmp_path / ".state", workspace_id=workspace_id)
    await repo.initialize()
    return repo


async def _build_executor(
    *,
    graph: Graph,
    llm: _FakeLLM,
    state_repo: StateRepo,
    graph_session_id: str,
    agents: dict[str, Agent] | None = None,
    tool_manager_resolver=None,
    graph_resolver=None,
) -> WorkspaceGraphExecutor:
    if agents is None:
        agents = {}

    async def agent_resolver(agent_id: str) -> Agent:
        return agents[agent_id]

    async def llm_resolver(agent: Agent):
        return (llm, _model())

    return WorkspaceGraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=state_repo,
        graph_session_id=graph_session_id,
        tool_manager_resolver=tool_manager_resolver,
        graph_resolver=graph_resolver,
    )


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


def _git_log_oneline(repo_path: Path) -> list[str]:
    """Return one-line commit subjects for the state repo, newest-first."""
    out = subprocess.check_output(
        ["git", "-C", str(repo_path), "log", "--format=%s"],
        text=True,
    )
    return [line for line in out.splitlines() if line.strip()]


# ===========================================================================
# Persistence
# ===========================================================================


class TestPersistence:
    @pytest.mark.asyncio
    async def test_persists_per_node_messages_jsonl(self, tmp_path: Path) -> None:
        graph = Graph(
            id="g-ws",
            description="A -> exit",
            entry_node_id="begin",
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(id="A", agent_id="x"),
                _EndNode(id="exit"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _StaticEdge(from_node="A", to_node="exit"),
            ],
        )
        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text="hello", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        )
        repo = await _make_state_repo(tmp_path)
        executor = await _build_executor(
            graph=graph,
            llm=llm,
            state_repo=repo,
            graph_session_id="gsid-1",
            agents={"x": _agent("x")},
        )
        await _drain(executor.invoke([]))
        msgs_path = executor.state_root / "nodes" / "A" / "messages.jsonl"
        assert msgs_path.exists()
        lines = [
            line for line in msgs_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 2
        parsed = [Message.model_validate_json(line) for line in lines]
        assert parsed[0].role == "user"
        assert parsed[1].role == "assistant"

    @pytest.mark.asyncio
    async def test_state_json_written_with_final_status(
        self, tmp_path: Path
    ) -> None:
        graph = Graph(
            id="g-ws",
            description="A -> exit",
            entry_node_id="begin",
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(id="A", agent_id="x"),
                _EndNode(id="exit"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _StaticEdge(from_node="A", to_node="exit"),
            ],
        )
        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text="hi", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        )
        repo = await _make_state_repo(tmp_path)
        executor = await _build_executor(
            graph=graph,
            llm=llm,
            state_repo=repo,
            graph_session_id="gsid-2",
            agents={"x": _agent("x")},
        )
        await _drain(executor.invoke([]))
        state_path = executor.state_root / "state.json"
        assert state_path.exists()
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        assert payload["status"] == "ended"
        assert payload["ended_reason"] == "completed"
        assert "A" in payload["node_states"]

    @pytest.mark.asyncio
    async def test_load_state_round_trip(self, tmp_path: Path) -> None:
        graph = Graph(
            id="g-ws",
            description="A -> exit",
            entry_node_id="begin",
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(id="A", agent_id="x"),
                _EndNode(id="exit"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _StaticEdge(from_node="A", to_node="exit"),
            ],
        )
        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text="hi", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        )
        repo = await _make_state_repo(tmp_path)
        executor = await _build_executor(
            graph=graph,
            llm=llm,
            state_repo=repo,
            graph_session_id="gsid-3",
            agents={"x": _agent("x")},
        )
        # Before invoke: load_state returns None.
        assert await executor.load_state() is None
        await _drain(executor.invoke([]))
        loaded = await executor.load_state()
        assert loaded is not None
        assert loaded["status"] == "ended"

    @pytest.mark.asyncio
    async def test_write_graph_binding(self, tmp_path: Path) -> None:
        graph = Graph(
            id="g-snap",
            description="snapshot test",
            entry_node_id="begin",
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(id="A", agent_id="x"),
            ],
            edges=[_StaticEdge(from_node="begin", to_node="A")],
        )
        llm = _FakeLLM(scripts=[[]])
        repo = await _make_state_repo(tmp_path)
        executor = await _build_executor(
            graph=graph,
            llm=llm,
            state_repo=repo,
            graph_session_id="gsid-snap",
            agents={"x": _agent("x")},
        )
        await executor.write_graph_binding()
        snap_path = executor.state_root / "graph.json"
        assert snap_path.exists()
        snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
        assert snapshot["id"] == "g-snap"
        assert snapshot["entry_node_id"] == "begin"


# ===========================================================================
# Cycle accumulates per-node history
# ===========================================================================


class TestCycleHistoryAccumulates:
    @pytest.mark.asyncio
    async def test_cycle_accumulates_messages_under_same_node(
        self, tmp_path: Path
    ) -> None:
        graph = Graph(
            id="g-loop",
            description="A -> A bounded",
            entry_node_id="begin",
            # +1 vs. the legacy fixture because the Begin step counts as
            # iteration 0 in the executor's superstep loop.
            max_iterations=4,
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(id="A", agent_id="x"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _StaticEdge(from_node="A", to_node="A"),
            ],
        )
        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text="loop", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        )
        repo = await _make_state_repo(tmp_path)
        executor = await _build_executor(
            graph=graph,
            llm=llm,
            state_repo=repo,
            graph_session_id="gsid-loop",
            agents={"x": _agent("x")},
        )
        await _drain(executor.invoke([]))
        msgs_path = executor.state_root / "nodes" / "A" / "messages.jsonl"
        lines = [
            line for line in msgs_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 6


# ===========================================================================
# Git-versioned state (every turn-end creates a commit)
# ===========================================================================


class TestGitVersioning:
    @pytest.mark.asyncio
    async def test_each_turn_end_produces_commit(self, tmp_path: Path) -> None:
        graph = Graph(
            id="g-ws",
            description="A -> exit",
            entry_node_id="begin",
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(id="A", agent_id="x"),
                _EndNode(id="exit"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _StaticEdge(from_node="A", to_node="exit"),
            ],
        )
        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text="hi", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        )
        repo = await _make_state_repo(tmp_path)
        executor = await _build_executor(
            graph=graph,
            llm=llm,
            state_repo=repo,
            graph_session_id="gsid-git",
            agents={"x": _agent("x")},
        )
        await _drain(executor.invoke([]))
        commits = _git_log_oneline(repo.path)
        assert len(commits) >= 3
        assert any("graph gsid-git: state @ iter" in c for c in commits)
        assert any("graph gsid-git: node A turn" in c for c in commits)
        assert any("(ended)" in c for c in commits), commits

    @pytest.mark.asyncio
    async def test_history_grep_by_graph_session(
        self, tmp_path: Path
    ) -> None:
        graph = Graph(
            id="g-ws",
            description="A -> exit",
            entry_node_id="begin",
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(id="A", agent_id="x"),
                _EndNode(id="exit"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _StaticEdge(from_node="A", to_node="exit"),
            ],
        )
        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text="hi", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        )
        repo = await _make_state_repo(tmp_path)
        executor = await _build_executor(
            graph=graph,
            llm=llm,
            state_repo=repo,
            graph_session_id="gsid-grep",
            agents={"x": _agent("x")},
        )
        await _drain(executor.invoke([]))
        out = subprocess.check_output(
            [
                "git", "-C", str(repo.path), "log",
                "--grep=^X-Primer-Graph: gsid-grep$",
                "--format=%s",
            ],
            text=True,
        )
        matched = [line for line in out.splitlines() if line.strip()]
        assert len(matched) >= 1


# ===========================================================================
# Tool dispatch in graph nodes (parity with standalone agents)
# ===========================================================================


class TestToolDispatchInGraphNode:
    @pytest.mark.asyncio
    async def test_node_calls_tool_then_continues(
        self, tmp_path: Path
    ) -> None:
        """Graph node should run the same tool-loop as a standalone agent."""
        graph = Graph(
            id="g-tools",
            description="A -> exit, agent calls a tool first",
            entry_node_id="begin",
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(id="A", agent_id="x"),
                _EndNode(id="exit"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _StaticEdge(from_node="A", to_node="exit"),
            ],
        )
        llm = _FakeLLM(
            scripts=[
                [
                    # LLM emits the scoped tool id (toolset_id__bare_name);
                    # ToolExecutionManager splits and forwards the bare
                    # name "echo" to the provider's call().
                    ToolCallStart(id="call-1", name="fake__echo", index=0),
                    ToolCallEnd(id="call-1", arguments={"x": 1}, index=0),
                    Done(stop_reason="tool_use", raw_reason="tool_use"),
                ],
                [
                    TextDelta(text="done", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ],
            ]
        )
        provider = _FakeToolsetProvider(tool_id="echo", output="echo-out")

        async def tool_mgr_resolver(agent: Agent) -> ToolExecutionManager:
            return ToolExecutionManager(toolset_providers={"fake": provider})

        repo = await _make_state_repo(tmp_path)
        executor = await _build_executor(
            graph=graph,
            llm=llm,
            state_repo=repo,
            graph_session_id="gsid-tools",
            agents={"x": _agent("x")},
            tool_manager_resolver=tool_mgr_resolver,
        )
        await _drain(executor.invoke([]))

        assert len(provider.calls) == 1
        assert provider.calls[0]["tool_name"] == "echo"

        # LLM streamed twice: once before tool call, once after.
        assert len(llm.calls) == 2

        msgs_path = executor.state_root / "nodes" / "A" / "messages.jsonl"
        assert msgs_path.exists()
        lines = [
            line for line in msgs_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        roles = [Message.model_validate_json(line).role for line in lines]
        assert roles == ["user", "assistant", "tool", "assistant"]


# ===========================================================================
# Subgraph execution
# ===========================================================================


class TestSubgraphExecution:
    @pytest.mark.asyncio
    async def test_subgraph_runs_and_persists_independently(
        self, tmp_path: Path
    ) -> None:
        """Subgraph node resolves a Graph and runs it via a child executor."""
        inner_graph = Graph(
            id="inner",
            description="single agent then exit",
            entry_node_id="begin",
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(id="inner-A", agent_id="x"),
                _EndNode(id="inner-exit"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="inner-A"),
                _StaticEdge(from_node="inner-A", to_node="inner-exit"),
            ],
        )
        outer_graph = Graph(
            id="outer",
            description="subgraph then exit",
            entry_node_id="begin",
            nodes=[
                _BeginNode(id="begin"),
                _GraphNodeRef(id="SUB", graph_id="inner"),
                _EndNode(id="exit"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="SUB"),
                _StaticEdge(from_node="SUB", to_node="exit"),
            ],
        )
        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text="from-inner", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        )

        async def graph_resolver(graph_id: str) -> Graph:
            assert graph_id == "inner"
            return inner_graph

        repo = await _make_state_repo(tmp_path)
        executor = await _build_executor(
            graph=outer_graph,
            llm=llm,
            state_repo=repo,
            graph_session_id="gsid-outer",
            agents={"x": _agent("x")},
            graph_resolver=graph_resolver,
        )
        await _drain(executor.invoke([]))

        outer_state = executor.state_root / "state.json"
        assert outer_state.exists()
        outer_payload = json.loads(outer_state.read_text(encoding="utf-8"))
        assert outer_payload["status"] == "ended"
        assert outer_payload["ended_reason"] == "completed"

        inner_state = (
            repo.path / "graphs" / "gsid-outer__SUB" / "state.json"
        )
        assert inner_state.exists()
        inner_msgs = (
            repo.path / "graphs" / "gsid-outer__SUB"
            / "nodes" / "inner-A" / "messages.jsonl"
        )
        assert inner_msgs.exists()


# ===========================================================================
# Workspace augmentation (system_prompt fragment + workspace tools)
# ===========================================================================


class _FakeWorkspaceSession:
    """Minimal AgentSession-shaped double for augmentation tests."""

    workspace_id = "ws-fake"
    session_id = "sess-fake"
    agent_id = "agent-fake"
    system_prompt_fragment = "<<WORKSPACE FRAGMENT>>"

    def __init__(self) -> None:
        self.workspace_tools: list = []

    async def cache_output(self, text: str) -> str:
        return "/tmp/fake"


class TestWorkspaceAugmentation:
    @pytest.mark.asyncio
    async def test_system_prompt_includes_workspace_fragment(
        self, tmp_path: Path
    ) -> None:
        graph = Graph(
            id="g-aug",
            description="A -> exit, with workspace augmentation",
            entry_node_id="begin",
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(id="A", agent_id="x"),
                _EndNode(id="exit"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _StaticEdge(from_node="A", to_node="exit"),
            ],
        )
        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text="hi", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        )
        repo = await _make_state_repo(tmp_path)
        agent = Agent(
            id="x",
            description="agent x",
            model=AgentModel(provider_id="p", model_name="m"),
            system_prompt=["BASE PROMPT"],
        )

        async def agent_resolver(agent_id: str) -> Agent:
            return agent

        async def llm_resolver(_a: Agent):
            return (llm, _model())

        executor = WorkspaceGraphExecutor(
            graph=graph,
            agent_resolver=agent_resolver,
            llm_resolver=llm_resolver,  # type: ignore[arg-type]
            state_repo=repo,
            graph_session_id="gsid-aug",
            workspace_session=_FakeWorkspaceSession(),  # type: ignore[arg-type]
        )
        await _drain(executor.invoke([]))

        first_call_messages = llm.calls[0]["messages"]
        sys_msgs = [m for m in first_call_messages if m.role == "system"]
        assert len(sys_msgs) == 1
        sys_text = "".join(
            p.text for p in sys_msgs[0].parts if hasattr(p, "text")
        )
        assert "BASE PROMPT" in sys_text
        assert "<<WORKSPACE FRAGMENT>>" in sys_text
