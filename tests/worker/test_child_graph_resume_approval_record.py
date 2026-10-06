"""An agent session parked on a CHILD graph's value-yield leaf writes no approval record when it is answered.

An agent calls ``system__invoke_graph``; the child graph parks on a ``tool_call`` node that yields ``ask_user`` (or on
an agent node that yields it). The agent session's own park is labelled ``_approval``: that is the label every GRAPH park
carries (``primer/graph/_checkpoint.py``), whatever its leaf is. ``resume_engine_session`` took the label for a real
approval gate and wrote a ``ToolApprovalRecord`` for the reply; ``classify_approval_payload({"response": "blue"})`` is
not "approved", so an audit record of a REJECTED approval was written for what was an operator's ``ask_user`` answer.

The decision belongs to the INNERMOST frame: when it is a ``GraphFrame``, the child checkpoint's entry for the node that
yielded (``node_tcid``) says what the leaf is. A real approval gate there, a gate in a nested agent chain, and a flat
session park still write their record exactly once.

Driven through the REAL ``resume_engine_session`` and the real ``ParkedState`` round trip; the pool is a thin fake that
records what it is asked to write, and the continuation walk itself is replaced by a delivery (its own behaviour is
pinned in ``test_child_graph_value_yield_resume.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

import primer.toolset.misc  # noqa: F401  (registers the ask_user resume hook)
from primer.model.chat import ToolResultPart
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded
from primer.worker import continuation
from primer.worker.frames import AgentFrame, AgentResumeContext, GraphFrame
from primer.worker.session_resume_coordinator import resume_engine_session
from primer.worker.yield_runtime import ParkedState
from tests.conftest import _FakeStorageProvider

SID = "sess-agent"
NODE_TCID = "tc-node"


class _Bus:
    async def publish(self, key: str, payload: dict) -> None:
        return None


class _Pool:
    """What ``resume_engine_session`` asks of the pool on the nested-continuation path."""

    def __init__(self) -> None:
        self._storage = _FakeStorageProvider()
        self._event_bus = _Bus()
        self.records: list[dict] = []
        self.ended: list[str] = []
        self.continued = 0

    async def _end_session(self, session, *, reason: str):
        self.ended.append(reason)
        return f"ENDED:{reason}"

    async def _load_workspace_for_persist(self, workspace_id: str):
        return object()

    async def _build_agent_executor(self, session, workspace):
        return SimpleNamespace(_tool_manager=None)

    def _build_invocation_services(self, session, workspace, executor, tool_manager):
        return object()

    async def _write_approval_record_for_session(self, *, session, blob, payload):
        self.records.append(payload)

    async def _inject_resume_and_continue(self, session, executor, parked, tool_result_part):
        self.continued += 1
        return "CONTINUED"

    def _repark_continuation(self, session, parked, outcome):  # pragma: no cover - the walk is replaced
        raise AssertionError("no re-park in these tests")


def _checkpoint(*, toolcall: str | None = None, agent_yield: str | None = None) -> dict[str, Any]:
    """The child graph's checkpoint: one pending tool_call node and/or one parked agent node, named ``NODE_TCID``."""
    entry = {
        "node_id": "n1", "tool_call_id": NODE_TCID, "parked_event_key": f"x:{SID}:{NODE_TCID}", "arguments": {},
        "resume_metadata": {},
    }
    ck: dict[str, Any] = {"pending_toolcalls": [], "pending_agent_yields": [], "pending_dispatch": []}
    if toolcall is not None:
        ck["pending_toolcalls"].append({**entry, "tool_name": toolcall})
    if agent_yield is not None:
        ck["pending_agent_yields"].append({
            "node_id": "n1", "tool_call_id": NODE_TCID, "event_key": entry["parked_event_key"],
            "tool_name": agent_yield, "resume_metadata": {}, "llm_messages": [], "iteration": 0, "frames": [],
            "leaf": None,
        })
    return ck


def _agent_frame() -> AgentFrame:
    return AgentFrame(
        agent_id="sub", llm_messages=[], tool_call_id="agent-tc", depth=0,
        context=AgentResumeContext(session_id=SID, workspace_id="ws-1", chat_id=None, principal="p", tools=[]),
    )


def _graph_frame(checkpoint: dict[str, Any]) -> GraphFrame:
    return GraphFrame(graph_id="child", gsid="gs-1", checkpoint=checkpoint, tool_call_id="agent-tc", node_tcid=NODE_TCID)


def _session(frames: list, *, tool_name: str = "_approval") -> WorkspaceSession:
    parked_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    parked = ParkedState(
        yielded=Yielded(tool_name=tool_name, event_key=f"graph:{SID}", timeout=600.0, resume_metadata={}),
        llm_messages=[], turn_no=0, started_at=parked_at, tool_call_id="agent-tc",
        resume_event_payload={"response": "blue"}, frames=frames,
    )
    return WorkspaceSession(
        id=SID, workspace_id="ws-1", binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
        status=SessionStatus.RUNNING, created_at=datetime.now(timezone.utc), turn_no=0,
        parked_status="resumable", parked_event_key=f"graph:{SID}", parked_until=parked_at + timedelta(seconds=600),
        parked_at=parked_at, parked_state=parked.to_jsonable(),
    )


@pytest.fixture
def pool(monkeypatch) -> _Pool:
    async def deliver(frames, leaf, payload, services):
        return continuation.Deliver(tool_result=ToolResultPart(id="agent-tc", output="ok"))

    monkeypatch.setattr(continuation, "resume_continuation", deliver)
    return _Pool()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "checkpoint",
    [
        _checkpoint(toolcall="ask_user"),
        _checkpoint(agent_yield="ask_user"),
    ],
    ids=["tool_call-node-ask_user", "agent-node-ask_user"],
)
async def test_an_ask_user_answer_in_a_child_graph_writes_no_approval_record(pool, checkpoint) -> None:
    session = _session([_graph_frame(checkpoint)])

    out = await resume_engine_session(pool, None, session)  # type: ignore[arg-type]

    assert out == "CONTINUED" and pool.continued == 1, f"the resume did not continue: {out!r} {pool.ended}"
    assert pool.records == [], f"an approval record was written for an ask_user answer: {pool.records}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frames_for",
    [
        "child-graph-approval-gate",
        "child-graph-legacy-park-without-tool_name",
        "child-graph-legacy-agent-yield-without-tool_name",
        "nested-agent-chain-gate",
        "flat",
    ],
)
async def test_a_real_approval_gate_still_writes_its_record_exactly_once(pool, frames_for) -> None:
    if frames_for == "child-graph-approval-gate":
        frames = [_graph_frame(_checkpoint(toolcall="_approval"))]
    elif frames_for == "child-graph-legacy-park-without-tool_name":
        ck = _checkpoint(toolcall="_approval")
        ck["pending_toolcalls"][0].pop("tool_name")
        frames = [_graph_frame(ck)]
    elif frames_for == "child-graph-legacy-agent-yield-without-tool_name":
        # a parked agent node of a checkpoint written before ``tool_name`` was captured: nothing says it is a value
        # yield, so it falls through as an approval gate and keeps its record (an ``is not None`` -> truthiness or
        # ``!= "_approval"`` rewrite of the loop's condition would drop it)
        ck = _checkpoint(agent_yield="_approval")
        ck["pending_agent_yields"][0].pop("tool_name")
        frames = [_graph_frame(ck)]
    elif frames_for == "nested-agent-chain-gate":
        frames = [_agent_frame()]
    else:
        frames = []
    session = _session(frames)

    await resume_engine_session(pool, None, session)  # type: ignore[arg-type]

    assert len(pool.records) == 1, f"{frames_for}: the approval record was written {len(pool.records)} times"


@pytest.mark.asyncio
async def test_a_child_graph_frame_whose_node_is_not_in_its_checkpoint_keeps_the_old_behaviour(pool) -> None:
    """No matching pending entry: the label is all there is, so the record is written as before (it cannot be told
    apart from a gate whose entry was already consumed)."""
    session = _session([_graph_frame(_checkpoint())])

    await resume_engine_session(pool, None, session)  # type: ignore[arg-type]

    assert len(pool.records) == 1
