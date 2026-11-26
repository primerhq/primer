"""The top-level graph executor's run identity is bound by the production builder (lane K2 round 1b).

The builder builds the session's executor with the default run id ``turn-<turn_no>``; without the
session row's invocation in the identity, a reopened session - which bumps ``metadata.invocation``
but NOT ``turn_no``, and whose failed turn was released with ``success=False`` so ``turn_no`` was
never bumped either - matches the stale ``state.json`` the failed run left behind and resumes it
instead of running fresh from Begin. A reclaim of the same turn must keep resuming.

Both tests drive the worker's own builder (``pool._build_executor`` ->
``executor_builders.build_graph_executor``, the path every graph turn and every graph resume takes)
and seed the state subtree with the exact ``state.json`` the prior run's boundary commit would have
left (the pre-bind default run id for the reopen case, the bound run id for the reclaim case).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from primer.graph.base import _GraphTransitionEvent
from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.chat import Done, Message, StreamEvent, TextDelta
from primer.model.graph import Graph, _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge
from primer.model.model_profile import ModelProfileConfig
from primer.model.workspace_session import GraphSessionBinding, SessionStatus, WorkspaceSession
from primer.model_profile import ResolvedModel
from tests.worker.test_executor_builders_instructions import (
    _AgentSession,
    _AgentStore,
    _Workspace,
    _agent,
    _pool,
    _state_repo,
)

SID = "s-run"
TURN_NO = 5


class _Holder(_AgentSession):
    """The graph session's holder: the run ends it when the graph ends."""

    async def set_status(self, status, *, ended_reason=None) -> None:
        self.ended = (status, ended_reason)


class _Rows:
    def __init__(self, rows: dict[str, Any]) -> None:
        self._rows = rows

    async def get(self, row_id: str):
        return self._rows.get(row_id)


class _Storage:
    """The agent row only: the bound graph comes from the row's snapshot, and a run with no tools
    or external tools never reads anything else."""

    def __init__(self, agent: Agent) -> None:
        self._agents = _AgentStore(agent)

    def get_storage(self, model):
        if model is Agent:
            return self._agents
        return _Rows({})


class _CountingLLM:
    async def list_models(self):
        return ["m"]

    def __init__(self) -> None:
        self.calls = 0

    def stream(self, *, model: str, messages: list[Message], **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls += 1

        async def _events() -> AsyncIterator[StreamEvent]:
            yield TextDelta(text="done", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")

        return _events()


def _graph() -> Graph:
    return Graph(
        access=Access(public=True),
        id="g-run", description="begin -> n1 -> end",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="n1", agent_id="ag-1", input_template="do it"),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="n1"),
            _StaticEdge(from_node="n1", to_node="end"),
        ],
    )


def _row(metadata: dict[str, Any] | None) -> WorkspaceSession:
    return WorkspaceSession(
        id=SID, workspace_id="ws-1",
        binding=GraphSessionBinding(graph_id="g-run", graph_snapshot=_graph()),
        status=SessionStatus.RUNNING, created_at=datetime.now(timezone.utc),
        turn_no=TURN_NO, metadata=metadata or {},
    )


async def _seed_stale_state(repo, *, run_id: str) -> None:
    """Commit the ``state.json`` a run cut between supersteps left: the boundary the next
    superstep (n1) runs at, with the settled begin's output and nothing else decided."""
    payload = {
        "run_id": run_id,
        "iteration": 1,
        "status": "running",
        "ended_reason": None,
        "ended_detail": None,
        "node_states": {
            "begin": {"status": "ended", "last_run_iteration": 0,
                      "last_run_at": "2026-11-24T21:00:00+00:00", "error": None},
            "n1": {"status": "pending", "last_run_iteration": None,
                   "last_run_at": None, "error": None},
            "end": {"status": "pending", "last_run_iteration": None,
                    "last_run_at": None, "error": None},
        },
        "ready_set": ["n1"],
        "initial_input": [],
        "initial_input_kind": "value",
        "outputs": {"begin": {"text": "", "parsed": None, "history": [], "iteration": 0, "error": None}},
        "fanout_instances": {},
        "fanout_target_expected_count": {},
        "instance_to_spec": {},
        "fanout_drain_state": {},
        "max_iter_routed": False,
        "pending_ended_reason": None,
        "pending_ended_detail": None,
    }
    await repo.commit_arbitrary(
        summary=f"graph {SID}: state @ iter 1 (running)",
        files={f"graphs/{SID}/state.json": json.dumps(payload)},
        trailers={"X-Primer-Graph": SID, "X-Primer-Op": "state", "X-Primer-Graph-Status": "running"},
    )


async def _run(monkeypatch, tmp_path: Path, *, metadata: dict[str, Any] | None, stale_run_id: str):
    """Build the executor the way the worker does and run the turn; return the enter events, the
    node's LLM, and the run's final state.json."""
    pool = _pool(monkeypatch)
    llm = _CountingLLM()

    async def _resolve_llm(_agent_row, _override=None):
        return llm, ResolvedModel(
            profile_id="prov-1--m-1", provider_id="prov-1", model_name="m-1", context_length=8000,
            config=ModelProfileConfig(),
        )

    monkeypatch.setattr(pool, "_resolve_llm", _resolve_llm)
    monkeypatch.setattr(pool, "_storage", _Storage(_agent()))
    repo = await _state_repo(tmp_path)
    await _seed_stale_state(repo, run_id=stale_run_id)
    ws = _Workspace(SID, state_repo=repo)
    ws._session = _Holder(SID)
    driver = await pool._build_executor(_row(metadata), ws)

    enters: list[str] = []
    async for ev in driver._executor.invoke([]):
        if isinstance(ev, _GraphTransitionEvent) and ev.phase == "enter":
            enters.append(ev.node_id)
    assert driver.last_done_reason == "graph_ended", "the turn must complete either way"
    state = json.loads(await repo.read_state_file(f"graphs/{SID}/state.json"))
    return enters, llm, state


async def test_a_reopened_session_runs_fresh_not_the_stale_state_of_its_failed_turn(monkeypatch, tmp_path):
    """The failed turn's state.json carries the pre-bind default identity ``turn-<turn_no>`` (what
    production wrote before this bind); the reopen bumps the invocation while the failed release
    left turn_no unbumped, so the next turn must run fresh from Begin."""
    enters, llm, state = await _run(
        monkeypatch, tmp_path,
        metadata={"invocation": 2},
        stale_run_id=f"turn-{TURN_NO}",
    )

    assert "begin" in enters, "the reopened turn resumed the failed run's stale state instead of running fresh from Begin"
    assert llm.calls == 1
    assert state["run_id"] == f"2.{TURN_NO}"
    assert state["status"] == "ended" and state["ended_reason"] == "completed"


async def test_a_reclaim_of_the_same_turn_still_resumes(monkeypatch, tmp_path):
    """The same turn's own state - its bound identity ``<invocation>.<turn_no>`` - is resumed,
    not re-run from Begin: the settled begin does not run again."""
    enters, llm, state = await _run(
        monkeypatch, tmp_path,
        metadata=None,
        stale_run_id=f"1.{TURN_NO}",
    )

    assert "begin" not in enters, "the reclaim re-ran the settled begin instead of resuming the in-flight run"
    assert enters == ["n1", "end"]
    assert llm.calls == 1
    assert state["run_id"] == f"1.{TURN_NO}"
    assert state["status"] == "ended" and state["ended_reason"] == "completed"
