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
    FanOutSpec,
    Graph,
    JsonPathBranch,
    _AgentNodeRef,
    _BeginNode,
    _ConditionalEdge,
    _EndNode,
    _FanInNode,
    _FanOutNode,
    _GraphNodeRef,
    _JsonPathRouter,
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
        ctx=None,
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
        assert any(n.get("kind") == "begin" for n in snapshot["nodes"])


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
            # +1 vs. the legacy fixture because the Begin step counts as
            # iteration 0 in the executor's superstep loop.
            max_iterations=4,
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(
                    id="A",
                    agent_id="x",
                    response_format={"type": "object"},
                ),
                # Reachability declaration only — the loop below always
                # routes back to A so this End never actually fires.
                _EndNode(id="exit"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _ConditionalEdge(
                    from_node="A",
                    router=_JsonPathRouter(
                        branches=[
                            JsonPathBranch(conditions=[], to_node="A"),
                        ],
                        default_to="exit",
                    ),
                ),
            ],
        )
        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text='{"go": "a"}', index=0),
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


class _RecordingHolder:
    """AgentSession-shaped double that records ``set_status`` transitions.

    Stands in for the on-disk session holder slot so a test can assert
    the graph executor moves it to ENDED when the run terminates.
    """

    workspace_id = "ws-test"
    session_id = "sess-hold"
    agent_id = "graph:g"
    system_prompt_fragment = ""

    def __init__(self) -> None:
        self.workspace_tools: list = []
        self.status_calls: list[tuple] = []

    async def cache_output(self, text: str) -> str:
        return "/tmp/fake"

    async def set_status(self, status, *, ended_reason=None, waiting_state=None):
        self.status_calls.append((status, ended_reason))


def _begin_agent_end_graph() -> Graph:
    return Graph(
        id="g-hold",
        description="begin -> A -> exit",
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


def _one_shot_llm() -> "_FakeLLM":
    return _FakeLLM(
        scripts=[
            [
                TextDelta(text="hi", index=0),
                Done(stop_reason="stop", raw_reason="stop"),
            ]
        ]
    )


class TestHolderLifecycle:
    @pytest.mark.asyncio
    async def test_completed_graph_ends_holder_session_when_owned(
        self, tmp_path: Path
    ) -> None:
        """The root graph executor must transition the on-disk session
        holder to ENDED when the graph completes, so workspace-level
        session views (get/list_workspace_session) reflect the terminal
        state instead of reporting the session as perpetually running."""
        from primer.model.workspace_session import SessionStatus

        repo = await _make_state_repo(tmp_path)
        holder = _RecordingHolder()

        async def agent_resolver(agent_id: str) -> Agent:
            return _agent(agent_id)

        async def llm_resolver(_a: Agent):
            return (_one_shot_llm(), _model())

        executor = WorkspaceGraphExecutor(
            graph=_begin_agent_end_graph(),
            agent_resolver=agent_resolver,
            llm_resolver=llm_resolver,  # type: ignore[arg-type]
            state_repo=repo,
            graph_session_id="gsid-hold-1",
            workspace_session=holder,  # type: ignore[arg-type]
            owns_session_lifecycle=True,
        )
        await _drain(executor.invoke([]))

        assert (SessionStatus.ENDED, "completed") in holder.status_calls

    @pytest.mark.asyncio
    async def test_subgraph_executor_does_not_end_shared_holder(
        self, tmp_path: Path
    ) -> None:
        """A non-owning executor (e.g. a subgraph child sharing the
        parent's holder) must NOT end the holder when its own run
        terminates - that would kill the holder out from under the
        still-running parent graph."""
        repo = await _make_state_repo(tmp_path)
        holder = _RecordingHolder()

        async def agent_resolver(agent_id: str) -> Agent:
            return _agent(agent_id)

        async def llm_resolver(_a: Agent):
            return (_one_shot_llm(), _model())

        executor = WorkspaceGraphExecutor(
            graph=_begin_agent_end_graph(),
            agent_resolver=agent_resolver,
            llm_resolver=llm_resolver,  # type: ignore[arg-type]
            state_repo=repo,
            graph_session_id="gsid-hold-2",
            workspace_session=holder,  # type: ignore[arg-type]
            # owns_session_lifecycle defaults False
        )
        await _drain(executor.invoke([]))

        assert holder.status_calls == []


class TestStructuredNodeToolSuppression:
    @pytest.mark.asyncio
    async def test_response_format_node_offers_no_tools(self, tmp_path: Path) -> None:
        """A node with ``response_format`` must NOT offer tools to the LLM.

        A structured-output node is a data-shaping turn: it returns JSON
        matching the schema, it does not call tools. The workspace holder
        auto-injects tools into every node, and grammar-based providers
        (LM Studio / llama.cpp / Ollama) reject a forced json_schema
        combined with tools ('cannot combine structured output
        constraints with lazy grammar'), producing an empty stream. So
        when response_format is set the executor must suppress tools."""
        graph = Graph(
            id="g-sf",
            description="A(structured) -> exit",
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(
                    id="A", agent_id="x",
                    response_format={
                        "type": "object",
                        "properties": {"category": {"enum": ["bug", "feature"]}},
                    },
                ),
                _EndNode(id="exit", output_template="{{ nodes.A.text }}"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _StaticEdge(from_node="A", to_node="exit"),
            ],
        )
        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text='{"category":"bug"}', index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        )
        # The resolver hands back a manager that DOES expose a tool; the
        # executor must still call the LLM with an empty tool list because
        # the node demands structured output.
        provider = _FakeToolsetProvider(tool_id="echo", output="echo-out")

        async def tool_mgr_resolver(agent: Agent) -> ToolExecutionManager:
            return ToolExecutionManager(toolset_providers={"fake": provider})

        repo = await _make_state_repo(tmp_path)
        executor = await _build_executor(
            graph=graph,
            llm=llm,
            state_repo=repo,
            graph_session_id="gsid-sf",
            agents={"x": _agent("x")},
            tool_manager_resolver=tool_mgr_resolver,
        )
        await _drain(executor.invoke([]))

        assert len(llm.calls) == 1
        assert llm.calls[0].get("tools") == []
        assert provider.calls == []

    @pytest.mark.asyncio
    async def test_plain_node_still_offers_tools(self, tmp_path: Path) -> None:
        """Guard: a node WITHOUT response_format keeps its tools."""
        graph = Graph(
            id="g-plain",
            description="A(plain) -> exit",
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(id="A", agent_id="x"),
                _EndNode(id="exit", output_template="{{ nodes.A.text }}"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _StaticEdge(from_node="A", to_node="exit"),
            ],
        )
        llm = _FakeLLM(
            scripts=[[TextDelta(text="hi", index=0), Done(stop_reason="stop", raw_reason="stop")]]
        )
        provider = _FakeToolsetProvider(tool_id="echo", output="echo-out")

        async def tool_mgr_resolver(agent: Agent) -> ToolExecutionManager:
            return ToolExecutionManager(toolset_providers={"fake": provider})

        repo = await _make_state_repo(tmp_path)
        executor = await _build_executor(
            graph=graph,
            llm=llm,
            state_repo=repo,
            graph_session_id="gsid-plain",
            agents={"x": _agent("x")},
            tool_manager_resolver=tool_mgr_resolver,
        )
        await _drain(executor.invoke([]))

        assert len(llm.calls) == 1
        assert [t.id for t in llm.calls[0].get("tools") or []] == ["fake__echo"]


class TestTeeFanInAggregation:
    @pytest.mark.asyncio
    async def test_tee_target_aggregator_has_no_leading_none(
        self, tmp_path: Path
    ) -> None:
        """A tee target's fan-in aggregator must hold the target's output at
        index 0, not a leading None.

        Each tee target runs once (fanout_index is None). The aggregator
        accumulation pre-padded the list to ``(fanout_index or 0) + 1`` =
        one None and THEN appended, yielding ``nodes.<target> == [None,
        out]`` so ``nodes.<target>[0]`` was None and any fan-in template
        reading ``nodes.pros[0].text`` failed. The pad-with-None is only
        for indexed (broadcast/map) placement; tee must just append."""
        graph = Graph(
            id="g-tee-agg",
            description="begin -> tee[a,b] -> agents -> fan_in -> end",
            nodes=[
                _BeginNode(id="begin"),
                _FanOutNode(id="tee", specs=[
                    FanOutSpec(kind="tee", target_node_ids=["a", "b"]),
                ]),
                _AgentNodeRef(id="a", agent_id="x", input_template="A"),
                _AgentNodeRef(id="b", agent_id="x", input_template="B"),
                _FanInNode(
                    id="merge",
                    aggregate_template="{{ nodes.a[0].text }}|{{ nodes.b[0].text }}",
                ),
                _EndNode(id="exit", output_template="{{ nodes.merge.text }}"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="tee"),
                _StaticEdge(from_node="a", to_node="merge"),
                _StaticEdge(from_node="b", to_node="merge"),
                _StaticEdge(from_node="merge", to_node="exit"),
            ],
        )
        llm = _FakeLLM(
            scripts=[[TextDelta(text="hi", index=0), Done(stop_reason="stop", raw_reason="stop")]]
        )
        repo = await _make_state_repo(tmp_path)
        executor = await _build_executor(
            graph=graph,
            llm=llm,
            state_repo=repo,
            graph_session_id="gsid-tee-agg",
            agents={"x": _agent("x")},
        )
        await _drain(executor.invoke([]))

        state = await executor.load_state()
        assert state is not None
        assert state["status"] == "ended"
        assert state["ended_reason"] == "completed"


class TestToolCallInternalToolset:
    @pytest.mark.asyncio
    async def test_toolcall_resolves_internal_toolset(self, tmp_path: Path) -> None:
        """A tool_call node naming an internal-toolset tool (e.g.
        web__web-search, fake__echo) must resolve that toolset via the
        executor's ``toolset_resolver`` and dispatch it, not fail with
        'unknown tool ...; not registered with any toolset or workspace'.
        Previously tool_call nodes built a workspace-only manager
        (toolset_providers={}), so only workspace__* tools worked."""
        from primer.model.graph import _ToolCallNode

        provider = _FakeToolsetProvider(tool_id="echo", output="echo-out")

        async def toolset_resolver(toolset_id: str):
            assert toolset_id == "fake"
            return provider

        async def agent_resolver(agent_id: str) -> Agent:
            raise KeyError(agent_id)

        async def llm_resolver(agent: Agent):
            raise NotImplementedError

        repo = await _make_state_repo(tmp_path)
        executor = WorkspaceGraphExecutor(
            graph=Graph(
                id="g-tc",
                description="begin -> end (dispatch tested directly)",
                nodes=[_BeginNode(id="b"), _EndNode(id="e")],
                edges=[_StaticEdge(from_node="b", to_node="e")],
            ),
            agent_resolver=agent_resolver,
            llm_resolver=llm_resolver,  # type: ignore[arg-type]
            state_repo=repo,
            graph_session_id="gsid-tc",
            workspace_session=_FakeWorkspaceSession(),  # type: ignore[arg-type]
            toolset_resolver=toolset_resolver,
        )
        node = _ToolCallNode(id="t", tool_id="fake__echo", arguments={})
        result = await executor._dispatch_toolcall(node, {"x": 1})

        assert result.output == "echo-out"
        assert provider.calls and provider.calls[0]["tool_name"] == "echo"


class TestToolCallApprovalWiring:
    @pytest.mark.asyncio
    async def test_dispatch_toolcall_passes_approval_resolver(self, tmp_path, monkeypatch):
        """A graph ToolCall node must build its manager WITH the executor's
        approval_resolver, so a gated tool_call fires the approval gate
        (parks) instead of running ungated."""
        from primer.model.graph import _ToolCallNode
        from primer.model.chat import ToolResultPart
        from primer.agent import tool_manager as tm

        captured: dict = {}

        class _FakeMgr:
            async def execute(self, call, *, principal=None, bypass_approval=False):
                captured["bypass"] = bypass_approval
                return ToolResultPart(id=getattr(call, "id", "x"), output="ok")

        def _fake_for_workspace(cls, *, toolset_providers, session,
                                approval_resolver=None, provider_registry=None, tools=None):
            captured["approval_resolver"] = approval_resolver
            return _FakeMgr()

        monkeypatch.setattr(tm.ToolExecutionManager, "for_workspace",
                            classmethod(_fake_for_workspace))

        sentinel = object()
        repo = await _make_state_repo(tmp_path)

        async def _ar(agent_id): raise KeyError(agent_id)
        async def _lr(a): raise NotImplementedError

        executor = WorkspaceGraphExecutor(
            graph=Graph(id="g", description="d",
                        nodes=[_BeginNode(id="b"), _EndNode(id="e")],
                        edges=[_StaticEdge(from_node="b", to_node="e")]),
            agent_resolver=_ar, llm_resolver=_lr,  # type: ignore[arg-type]
            state_repo=repo, graph_session_id="gsid-ar",
            workspace_session=_FakeWorkspaceSession(),  # type: ignore[arg-type]
            approval_resolver=sentinel,
        )
        node = _ToolCallNode(id="t", tool_id="workspace__write", arguments={})
        await executor._dispatch_toolcall(node, {"path": "x"})
        assert captured["approval_resolver"] is sentinel

    @pytest.mark.asyncio
    async def test_with_bypass_redispatches_with_bypass_true(self, tmp_path):
        """On resume, _dispatch_toolcall_with_bypass must re-dispatch with
        bypass_approval=True so the gate does not re-fire (infinite park)."""
        from primer.model.graph import _ToolCallNode
        from primer.model.chat import ToolResultPart

        repo = await _make_state_repo(tmp_path)

        async def _ar(agent_id): raise KeyError(agent_id)
        async def _lr(a): raise NotImplementedError

        executor = WorkspaceGraphExecutor(
            graph=Graph(id="g", description="d",
                        nodes=[_BeginNode(id="b"), _EndNode(id="e")],
                        edges=[_StaticEdge(from_node="b", to_node="e")]),
            agent_resolver=_ar, llm_resolver=_lr,  # type: ignore[arg-type]
            state_repo=repo, graph_session_id="gsid-bp",
            workspace_session=_FakeWorkspaceSession(),  # type: ignore[arg-type]
        )
        seen: dict = {}

        async def _spy(node, arguments, *, bypass_approval=False):
            seen["bypass"] = bypass_approval
            return ToolResultPart(id="x", output="ok")

        executor._dispatch_toolcall = _spy  # type: ignore[assignment]
        node = _ToolCallNode(id="t", tool_id="workspace__write", arguments={})
        await executor._dispatch_toolcall_with_bypass(node, {})
        assert seen["bypass"] is True
