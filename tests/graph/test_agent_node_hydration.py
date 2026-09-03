"""A graph agent-node's run_agent_turn call gets artifact_storage from the
executor, so an artifact-backed part in the node's history is hydrated to
inline data before the LLM sees it -- same seam as the session path
(tests/agent/test_workspace_executor.py::TestAttachmentHydration), one
level deeper than _build_prompt: graph nodes build their own prompt list
and call run_agent_turn directly (primer/graph/_agent_node.py), never going
through AgentExecutor._build_prompt at all.

Reuses the capturing _FakeLLM + _make_state_repo + _build_executor from
tests.graph.test_workspace_executor, same pattern as
test_agent_node_system_prompt_ctx.py. History is seeded via
state_repo.commit_arbitrary directly (not _persist_node_turn, which only
buffers -- _load_node_history reads the committed repo state, so a
buffered-but-uncommitted write would be invisible to the node's own turn).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, ImagePart, Message, StreamEvent, TextDelta, TextPart
from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
from tests.graph.test_workspace_executor import (
    _FakeLLM,
    _build_executor,
    _make_state_repo,
)


class _MemArtifacts:
    def __init__(self) -> None:
        self.blobs: dict = {}

    async def put(self, *, data, mime_type, filename=None):
        from primer.int.artifact_storage import ArtifactBlob
        aid = f"art-{len(self.blobs) + 1}"
        self.blobs[aid] = ArtifactBlob(data=data, mime_type=mime_type, filename=filename)
        return aid

    async def get(self, artifact_id):
        return self.blobs.get(artifact_id)


async def _drain(it) -> list[StreamEvent]:
    return [ev async for ev in it]


def _graph() -> Graph:
    return Graph(
        id="g-hydration",
        description="Begin -> Agent -> End",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="A", agent_id="x", input_template="go"),
            _EndNode(id="end", output_template="{{ nodes.A.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="A"),
            _StaticEdge(from_node="A", to_node="end"),
        ],
    )


async def _seed_node_history(executor, node_id: str, messages: list[Message]) -> None:
    """Write + commit a node's messages.jsonl directly, bypassing the
    buffered _persist_node_turn/_save_state pair so the history is visible
    to _load_node_history (which reads committed repo state) before the
    executor's own invoke() runs."""
    rel_path = executor._state_rel(f"nodes/{node_id}/messages.jsonl")
    body = "\n".join(m.model_dump_json() for m in messages) + "\n"
    await executor._state_repo.commit_arbitrary(
        summary="test: seed node history", files={rel_path: body},
    )


@pytest.mark.asyncio
async def test_graph_agent_node_hydrates_artifact_backed_history(
    tmp_path: Path,
) -> None:
    arts = _MemArtifacts()
    aid = await arts.put(data=b"PNGBYTES", mime_type="image/png")
    repo = await _make_state_repo(tmp_path, workspace_id="ws-hydration")
    llm = _FakeLLM(
        scripts=[[
            TextDelta(text="ok", index=0),
            Done(stop_reason="stop", raw_reason="stop"),
        ]]
    )
    executor = await _build_executor(
        graph=_graph(),
        llm=llm,
        state_repo=repo,
        graph_session_id="gsid-hydration",
        agents={"x": Agent(id="x", description="x", model=AgentModel(profile_id="p--m"))},
        artifact_storage=arts,
    )
    await _seed_node_history(executor, "A", [
        Message(role="user", parts=[
            TextPart(text="earlier"),
            ImagePart(artifact_id=aid, mime_type="image/png"),
        ]),
    ])

    await _drain(executor.invoke([]))

    sent_parts = [
        p for call in llm.calls for m in call["messages"] for p in m.parts
        if isinstance(p, ImagePart)
    ]
    assert len(sent_parts) == 1
    assert sent_parts[0].data == b"PNGBYTES"
    assert sent_parts[0].artifact_id is None


@pytest.mark.asyncio
async def test_graph_agent_node_without_artifact_storage_is_unaffected(
    tmp_path: Path,
) -> None:
    """Every existing graph executor (no artifact_storage passed) keeps
    behaving exactly as before."""
    repo = await _make_state_repo(tmp_path, workspace_id="ws-hydration-2")
    llm = _FakeLLM(
        scripts=[[
            TextDelta(text="ok", index=0),
            Done(stop_reason="stop", raw_reason="stop"),
        ]]
    )
    executor = await _build_executor(
        graph=_graph(),
        llm=llm,
        state_repo=repo,
        graph_session_id="gsid-hydration-2",
        agents={"x": Agent(id="x", description="x", model=AgentModel(profile_id="p--m"))},
    )
    await _seed_node_history(executor, "A", [
        Message(role="user", parts=[
            TextPart(text="earlier"),
            ImagePart(artifact_id="art-orphan", mime_type="image/png"),
        ]),
    ])

    await _drain(executor.invoke([]))

    sent_parts = [
        p for call in llm.calls for m in call["messages"] for p in m.parts
        if isinstance(p, ImagePart)
    ]
    assert len(sent_parts) == 1
    assert sent_parts[0].artifact_id == "art-orphan"
    assert sent_parts[0].data is None
