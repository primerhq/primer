"""Tests for Task 1.3: executor state reads go through StateRepo.read_state_file.

Construct a WorkspaceGraphExecutor with a fake state_repo whose
read_state_file returns canned bytes for known paths. Point the executor
at a tmp dir that contains NO .state files. Verify that _load_node_history
and load_state return the canned data -- proving they route through the
fake, not the local filesystem.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Message, StreamEvent, TextPart
from primer.model.graph import Graph, _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge
from primer.model.provider import LLMModel


# ===========================================================================
# Fake state repo
# ===========================================================================


class _FakeStateRepo:
    """Minimal StateRepo that routes read_state_file to canned bytes."""

    def __init__(self, path: Path, *, canned: dict[str, bytes]) -> None:
        self._path = path
        self._canned = canned
        self.reads: list[str] = []

    @property
    def path(self) -> Path:
        return self._path

    async def read_state_file(self, path: str) -> bytes | None:
        self.reads.append(path)
        return self._canned.get(path)

    # Stubs for the constructor -- WorkspaceGraphExecutor only needs path and
    # read_state_file for _load_node_history / load_state, but the __init__
    # references path in the turn-log setup. Provide no-op commit_arbitrary
    # so any incidental call doesn't crash.
    async def commit_arbitrary(self, **kwargs: Any) -> str:
        return "fake-sha"


# ===========================================================================
# Helpers
# ===========================================================================

_GSID = "g-test-session"
_NODE_ID = "node-A"


def _messages_rel(gsid: str, node_id: str) -> str:
    return f"graphs/{gsid}/nodes/{node_id}/messages.jsonl"


def _state_rel(gsid: str) -> str:
    return f"graphs/{gsid}/state.json"


def _build_executor(
    *,
    state_repo: _FakeStateRepo,
    graph_session_id: str = _GSID,
) -> WorkspaceGraphExecutor:
    graph = Graph(
        id="g-test",
        description="minimal graph for state-read tests",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id=_NODE_ID, agent_id="agent-x"),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node=_NODE_ID),
            _StaticEdge(from_node=_NODE_ID, to_node="exit"),
        ],
    )

    async def agent_resolver(agent_id: str) -> Agent:
        return Agent(
            id=agent_id,
            description=f"agent {agent_id}",
            model=AgentModel(provider_id="p", model_name="m"),
        )

    async def llm_resolver(agent: Agent):
        return (None, LLMModel(name="m", context_length=128_000))  # type: ignore[arg-type]

    return WorkspaceGraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=state_repo,  # type: ignore[arg-type]
        graph_session_id=graph_session_id,
    )


# ===========================================================================
# Tests
# ===========================================================================


class TestLoadNodeHistoryRoutesThrough:
    @pytest.mark.asyncio
    async def test_returns_messages_from_state_repo(self, tmp_path: Path) -> None:
        msg = Message(role="user", parts=[TextPart(text="hello from state repo")])
        jsonl = msg.model_dump_json() + "\n"
        rel = _messages_rel(_GSID, _NODE_ID)
        fake_repo = _FakeStateRepo(
            tmp_path / ".state",
            canned={rel: jsonl.encode()},
        )
        executor = _build_executor(state_repo=fake_repo)

        result = await executor._load_node_history(_NODE_ID)

        assert len(result) == 1
        assert result[0].role == "user"
        assert isinstance(result[0].parts[0], TextPart)
        assert result[0].parts[0].text == "hello from state repo"

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_file_absent(self, tmp_path: Path) -> None:
        fake_repo = _FakeStateRepo(tmp_path / ".state", canned={})
        executor = _build_executor(state_repo=fake_repo)

        result = await executor._load_node_history(_NODE_ID)

        assert result == []

    @pytest.mark.asyncio
    async def test_no_local_filesystem_read(self, tmp_path: Path) -> None:
        """Canned bytes come from fake_repo; no .state dir exists on disk."""
        msg = Message(role="assistant", parts=[TextPart(text="canned")])
        jsonl = msg.model_dump_json() + "\n"
        rel = _messages_rel(_GSID, _NODE_ID)
        fake_repo = _FakeStateRepo(
            tmp_path / ".state",
            canned={rel: jsonl.encode()},
        )
        # Confirm the .state dir does NOT exist so a direct-FS read would fail.
        assert not (tmp_path / ".state").exists()

        executor = _build_executor(state_repo=fake_repo)
        result = await executor._load_node_history(_NODE_ID)

        assert len(result) == 1
        assert isinstance(result[0].parts[0], TextPart)
        assert result[0].parts[0].text == "canned"
        # state_repo.read_state_file was called with the right path.
        assert rel in fake_repo.reads


class TestLoadStateRoutesThrough:
    @pytest.mark.asyncio
    async def test_returns_state_from_state_repo(self, tmp_path: Path) -> None:
        payload = {"iteration": 3, "status": "running", "node_states": {}}
        rel = _state_rel(_GSID)
        fake_repo = _FakeStateRepo(
            tmp_path / ".state",
            canned={rel: json.dumps(payload).encode()},
        )
        executor = _build_executor(state_repo=fake_repo)

        result = await executor.load_state()

        assert result is not None
        assert result["iteration"] == 3
        assert result["status"] == "running"

    @pytest.mark.asyncio
    async def test_returns_none_when_file_absent(self, tmp_path: Path) -> None:
        fake_repo = _FakeStateRepo(tmp_path / ".state", canned={})
        executor = _build_executor(state_repo=fake_repo)

        result = await executor.load_state()

        assert result is None

    @pytest.mark.asyncio
    async def test_no_local_filesystem_read(self, tmp_path: Path) -> None:
        """Canned bytes come from fake_repo; no .state dir exists on disk."""
        payload = {"iteration": 1, "status": "ended", "node_states": {}}
        rel = _state_rel(_GSID)
        fake_repo = _FakeStateRepo(
            tmp_path / ".state",
            canned={rel: json.dumps(payload).encode()},
        )
        # Confirm the .state dir does NOT exist.
        assert not (tmp_path / ".state").exists()

        executor = _build_executor(state_repo=fake_repo)
        result = await executor.load_state()

        assert result is not None
        assert result["status"] == "ended"
        assert rel in fake_repo.reads
