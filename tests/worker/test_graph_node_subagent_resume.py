"""Task 6.1 - RESUME side: the worker's graph-session resume runs the
continuation walk for a node parked on a nested invoke_agent yield, then either
delivers the unwound result into the node (Deliver) or re-parks the graph
session (Repark).

These exercise ``WorkerPool._resume_graph_continuation`` /
``_repark_graph_continuation`` / ``_graph_nested_agent_yield`` directly on a
bare pool instance (``__new__``) with fakes, since the full ``_resume_graph_engine``
pulls in storage / executor build. The branch selection + delivery contract is
what's load-bearing here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import primer.worker.continuation as continuation_mod
from primer.model.chat import ToolResultPart
from primer.model.yield_ import Yielded, YieldToWorker
from primer.worker.continuation import Deliver, Repark
from primer.worker.frames import AgentFrame, AgentResumeContext, frames_to_jsonable
from primer.worker.pool import WorkerPool


_INVOKE_TCID = "invoke-tc"
_LEAF_TCID = "leaf-tc"


def _frame() -> AgentFrame:
    return AgentFrame(
        agent_id="sub",
        llm_messages=[{"role": "assistant", "parts": []}],
        tool_call_id=_INVOKE_TCID,
        depth=0,
        context=AgentResumeContext(
            session_id="s", workspace_id="w", chat_id=None,
            principal="p", tools=["misc__ask_user"],
        ),
    )


def _checkpoint_with_nested_entry():
    return {
        "pending_agent_yields": [
            {
                "node_id": "A",
                "tool_call_id": _LEAF_TCID,
                "event_key": f"ask_user:s:{_LEAF_TCID}",
                "tool_name": "ask_user",
                "resume_metadata": {"prompt": "color?"},
                "llm_messages": [{"role": "assistant", "parts": []}],
                "iteration": 0,
                "frames": frames_to_jsonable([_frame()]),
                "leaf": Yielded(
                    tool_name="ask_user",
                    event_key=f"ask_user:s:{_LEAF_TCID}",
                    resume_metadata={"prompt": "color?"},
                ).to_jsonable(),
            }
        ],
    }


def _bare_pool():
    pool = WorkerPool.__new__(WorkerPool)
    pool._storage = None
    pool._provider_registry = None
    pool._approval_resolver = None
    return pool


def _session():
    return SimpleNamespace(
        id="sess", workspace_id="w", turn_no=3, binding=SimpleNamespace(kind="graph"),
    )


def _parked():
    return SimpleNamespace(tool_call_id=_INVOKE_TCID)


# ---------------------------------------------------------------------------
# branch selection
# ---------------------------------------------------------------------------


def test_nested_agent_yield_detected_only_with_frames():
    pool = _bare_pool()
    ck = _checkpoint_with_nested_entry()
    assert pool._graph_nested_agent_yield(ck, _LEAF_TCID) is not None
    # An entry with NO frames is NOT a nested yield (ordinary ask_user path).
    ck["pending_agent_yields"][0]["frames"] = []
    assert pool._graph_nested_agent_yield(ck, _LEAF_TCID) is None
    # Unknown tcid -> None.
    assert pool._graph_nested_agent_yield(_checkpoint_with_nested_entry(), "nope") is None


# ---------------------------------------------------------------------------
# Deliver: the continuation result becomes the node's agent_tool_result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continuation_deliver_builds_node_agent_tool_result(monkeypatch):
    pool = _bare_pool()
    # Stub services build (no real storage) + the walk -> Deliver.
    monkeypatch.setattr(pool, "_build_invocation_services",
                        lambda *a, **k: object())

    # The unwound result is keyed by the INVOKE_AGENT call id (outermost frame).
    delivered = ToolResultPart(id=_INVOKE_TCID, output='{"output": "blue"}',
                               error=False)

    async def _fake_walk(frames, leaf, payload, services):
        # The walk receives the rehydrated frames + leaf from the checkpoint.
        assert len(frames) == 1 and frames[0].tool_call_id == _INVOKE_TCID
        assert isinstance(leaf, Yielded) and leaf.tool_name == "ask_user"
        assert payload == {"answer": "blue"}
        return Deliver(tool_result=delivered)

    monkeypatch.setattr(continuation_mod, "resume_continuation", _fake_walk)

    ck = _checkpoint_with_nested_entry()
    ay = pool._graph_nested_agent_yield(ck, _LEAF_TCID)
    cont = await pool._resume_graph_continuation(
        _session(), _parked(), ck, ay, {"answer": "blue"},
        workspace=object(), executor=object(),
    )
    assert cont.repark_outcome is None
    msg = cont.agent_tool_result
    assert msg is not None and msg.role == "tool"
    # The node's agent_tool_result pairs against the invoke_agent tool_use.
    assert msg.parts[0].id == _INVOKE_TCID
    assert msg.parts[0].output == '{"output": "blue"}'


# ---------------------------------------------------------------------------
# Repark: a frame re-yielded -> re-park the GRAPH SESSION on the new leaf
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continuation_repark_reparks_graph_session(monkeypatch):
    pool = _bare_pool()
    monkeypatch.setattr(pool, "_build_invocation_services",
                        lambda *a, **k: object())

    new_leaf = Yielded(tool_name="ask_user", event_key="ask_user:s:leaf2",
                       resume_metadata={"prompt": "again?"})

    async def _fake_walk(frames, leaf, payload, services):
        return Repark(frames=[_frame()], leaf=new_leaf)

    monkeypatch.setattr(continuation_mod, "resume_continuation", _fake_walk)

    ck = _checkpoint_with_nested_entry()
    ay = pool._graph_nested_agent_yield(ck, _LEAF_TCID)
    cont = await pool._resume_graph_continuation(
        _session(), _parked(), ck, ay, {"answer": "x"},
        workspace=object(), executor=object(),
    )
    assert cont.agent_tool_result is None
    outcome = cont.repark_outcome
    assert outcome is not None and outcome.park is not None
    # Re-parked on the NEW leaf's event key, NOT the original.
    assert outcome.park.parked_event_key == "ask_user:s:leaf2"
    # The graph_checkpoint is preserved (graph did NOT advance) but the entry's
    # frames/leaf are updated to the new continuation.
    new_ck = outcome.park.parked_state["graph_checkpoint"]
    entry = new_ck["pending_agent_yields"][0]
    assert entry["event_key"] == "ask_user:s:leaf2"
    assert entry["leaf"]["event_key"] == "ask_user:s:leaf2"
    assert len(entry["frames"]) == 1
    # The session turn is preserved; the lease is dropped (parked).
    assert outcome.drop_lease is True
    assert outcome.park.parked_at is not None


@pytest.mark.asyncio
async def test_continuation_real_walk_resumes_subagent_then_delivers(monkeypatch):
    """End-to-end through the REAL continuation walk: the subagent frame's
    resume runs (services.resume_subagent), produces its final text, and the
    walk Delivers a tool_result keyed by the invoke_agent call id -> the node's
    agent_tool_result."""
    from primer.worker.continuation import InvocationServices

    async def _resume_subagent(*, agent_id, context, llm_messages, child_result,
                               depth, invoke_tool_call_id):
        # The subagent resumes with the ask_user answer (child_result) and runs
        # to its final assistant text.
        assert agent_id == "sub"
        assert invoke_tool_call_id == _INVOKE_TCID
        assert child_result.output  # the ask_user answer threaded in
        return "the answer is blue"

    services = InvocationServices(
        build_subagent_toolmanager=None,
        resume_subagent=_resume_subagent,
        resolve_graph=None,
        build_child_graph_executor=None,
        graph_agent_tool_result=None,
    )
    pool = _bare_pool()
    monkeypatch.setattr(pool, "_build_invocation_services",
                        lambda *a, **k: services)

    ck = _checkpoint_with_nested_entry()
    ay = pool._graph_nested_agent_yield(ck, _LEAF_TCID)
    cont = await pool._resume_graph_continuation(
        _session(), _parked(), ck, ay, {"answer": "blue"},
        workspace=object(), executor=object(),
    )
    assert cont.repark_outcome is None
    msg = cont.agent_tool_result
    assert msg.parts[0].id == _INVOKE_TCID
    import json
    assert json.loads(msg.parts[0].output) == {"output": "the answer is blue"}


def test_repark_preserves_invoke_tcid_on_parked_state():
    """The re-park's ParkedState.tool_call_id stays the invoke_agent call id so
    the eventual completion pairs the node delivery correctly."""
    pool = _bare_pool()
    ck = _checkpoint_with_nested_entry()
    ay = pool._graph_nested_agent_yield(ck, _LEAF_TCID)
    leaf = Yielded(tool_name="ask_user", event_key="ask_user:s:leaf3")
    outcome = pool._repark_graph_continuation(
        _session(), _parked(), ck, ay,
        Repark(frames=[_frame()], leaf=leaf),
    )
    assert outcome.park.parked_state["tool_call_id"] == _INVOKE_TCID
