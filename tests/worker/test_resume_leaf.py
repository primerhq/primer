"""Tests for per-frame ``resume_leaf`` (Task 3.3c).

The INNERMOST frame on the continuation stack resolves its OWN leaf
polymorphically:

* :class:`AgentFrame.resume_leaf` defers to :func:`frames.apply_leaf` (the
  leaf belongs to the subagent's own tool call). If that re-parks it
  propagates the :class:`Reparked`; otherwise it threads the resolved
  :class:`ToolResultPart` straight into ``self.resume`` to continue the
  subagent turn.
* :class:`GraphFrame.resume_leaf` resolves the child graph executor and
  delegates to :func:`frames.resume_invoke_graph` (only the graph's own
  resume can resolve a leaf belonging to a node INSIDE the child graph),
  passing BOTH the raw payload AND the computed ``agent_tool_result``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

import primer.worker.frames as frames_mod
from primer.model.chat import ToolResultPart
from primer.worker.frames import (
    AgentFrame,
    AgentResumeContext,
    Completed,
    GraphFrame,
    Reparked,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx() -> AgentResumeContext:
    return AgentResumeContext(
        session_id="s1",
        workspace_id="w1",
        chat_id=None,
        principal="p1",
        tools=["misc__ask_user"],
    )


def _agent_frame() -> AgentFrame:
    return AgentFrame(
        agent_id="sub",
        llm_messages=[],
        tool_call_id="agent-tc",
        depth=1,
        context=_ctx(),
    )


def _graph_frame() -> GraphFrame:
    return GraphFrame(
        graph_id="g1",
        gsid="gsid-1",
        checkpoint={"pending_agent_yields": []},
        tool_call_id="graph-tc",
        node_tcid="node-tc",
    )


@dataclass
class _Leaf:
    tool_name: str = "misc__ask_user"
    resume_metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# AgentFrame.resume_leaf
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_resume_leaf_completed_threads_into_resume(monkeypatch):
    frame = _agent_frame()
    leaf_answer = ToolResultPart(id="agent-tc", output="leaf-answer", error=False)
    final = Completed(value=ToolResultPart(id="agent-tc", output="sub-done", error=False))

    seen = {}

    async def fake_apply_leaf(inner_frame, leaf, payload, services):
        seen["apply_leaf"] = (inner_frame, leaf, payload, services)
        return leaf_answer

    async def fake_resume(child_result, services):
        seen["resume"] = (child_result, services)
        return final

    monkeypatch.setattr(frames_mod, "apply_leaf", fake_apply_leaf)
    monkeypatch.setattr(frame, "resume", fake_resume)

    leaf = _Leaf()
    out = await frame.resume_leaf(leaf, payload={"a": 1}, services="SVC")

    assert out is final
    # apply_leaf was called with this frame as the innermost.
    assert seen["apply_leaf"][0] is frame
    assert seen["apply_leaf"][1] is leaf
    # The resolved leaf answer was threaded into resume.
    assert seen["resume"][0] is leaf_answer
    assert seen["resume"][1] == "SVC"


@pytest.mark.asyncio
async def test_agent_resume_leaf_reparked_does_not_resume(monkeypatch):
    frame = _agent_frame()
    repark = Reparked(new_yield=object())

    async def fake_apply_leaf(inner_frame, leaf, payload, services):
        return repark

    resumed = {"called": False}

    async def fake_resume(child_result, services):
        resumed["called"] = True
        return Completed(value=None)

    monkeypatch.setattr(frames_mod, "apply_leaf", fake_apply_leaf)
    monkeypatch.setattr(frame, "resume", fake_resume)

    out = await frame.resume_leaf(_Leaf(), payload={}, services="SVC")

    assert out is repark
    assert resumed["called"] is False


# ---------------------------------------------------------------------------
# GraphFrame.resume_leaf
# ---------------------------------------------------------------------------


def _graph_services(recorder):
    @dataclass
    class _Svc:
        async def resolve_graph(self, graph_id):
            recorder["resolve_graph"] = graph_id
            return f"graph::{graph_id}"

        async def build_child_graph_executor(self, graph, gsid):
            recorder["build_child"] = (graph, gsid)
            return f"child::{gsid}"

        async def graph_agent_tool_result(self, checkpoint, tcid, payload):
            recorder["agent_tool_result"] = (checkpoint, tcid, payload)
            return "ATR"

    return _Svc()


@pytest.mark.asyncio
async def test_graph_resume_leaf_completed(monkeypatch):
    frame = _graph_frame()
    rec: dict[str, Any] = {}
    services = _graph_services(rec)

    async def fake_resume_invoke_graph(*, child, checkpoint, payload, resumed_tcid, agent_tool_result):
        rec["resume_invoke_graph"] = {
            "child": child,
            "checkpoint": checkpoint,
            "payload": payload,
            "resumed_tcid": resumed_tcid,
            "agent_tool_result": agent_tool_result,
        }
        return "graph out", None

    monkeypatch.setattr(frames_mod, "resume_invoke_graph", fake_resume_invoke_graph)

    out = await frame.resume_leaf(_Leaf(), payload={"p": 9}, services=services)

    assert isinstance(out, Completed)
    # The result pairs with the AGENT's invoke_graph call id (tool_call_id).
    assert out.value == ToolResultPart(
        id="graph-tc", output='{"output": "graph out"}', error=False,
    )
    # graph_agent_tool_result awaited with (checkpoint, child node tcid, payload).
    assert rec["agent_tool_result"] == (frame.checkpoint, frame.node_tcid, {"p": 9})
    # resume_invoke_graph got the resolved child + raw payload + the ATR, and
    # resumed_tcid is the CHILD graph's parked-node id (node_tcid), not the
    # agent's call id.
    rig = rec["resume_invoke_graph"]
    assert rig["child"] == "child::gsid-1"
    assert rig["checkpoint"] is frame.checkpoint
    assert rig["payload"] == {"p": 9}
    assert rig["resumed_tcid"] == "node-tc"
    assert rig["agent_tool_result"] == "ATR"


@pytest.mark.asyncio
async def test_graph_resume_leaf_reparked(monkeypatch):
    frame = _graph_frame()
    rec: dict[str, Any] = {}
    services = _graph_services(rec)
    repark = object()

    async def fake_resume_invoke_graph(**kwargs):
        return None, repark

    monkeypatch.setattr(frames_mod, "resume_invoke_graph", fake_resume_invoke_graph)

    out = await frame.resume_leaf(_Leaf(), payload={}, services=services)

    assert isinstance(out, Reparked)
    assert out.new_yield is repark
