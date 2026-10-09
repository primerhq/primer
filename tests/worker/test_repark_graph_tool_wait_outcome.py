"""``repark_graph_outcome`` / ``_repark_graph_tool_wait_outcome`` (Phase 3
stage 7a, 01a0518b boundary d) - direct unit tests for the ToolWaitPark-
shaped repark builder. Exercised end-to-end by the graph order tests,
but its own output shape (per-node wake keys, node_tool_call_seq
threading, notifying-result inclusion) had no direct test pinning it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from primer.model.chat import ToolResultPart
from primer.model.workspace_session import (
    AgentSessionBinding, SessionStatus, WorkspaceSession,
)
from primer.model.yield_ import ToolWaitPark
from primer.worker.graph_resume_coordinator import repark_graph_outcome
from primer.worker.yield_runtime import ToolWaitParkedState


def _session(turn_no: int = 0) -> WorkspaceSession:
    return WorkspaceSession(
        id="gs-1", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=datetime.now(timezone.utc),
        turn_no=turn_no,
    )


def test_dispatches_to_tool_wait_builder_for_toolwaitpark() -> None:
    session = _session()
    repark = ToolWaitPark(
        outstanding_task_ids=["A:tool:0:1"], event_key="tool_wait:A:tool:0:1",
        notifying_results=[],
    )
    repark.graph_checkpoint = {
        "pending_tool_waits": [
            # as the park materializer stores it: the session-qualified form (S1b)
            {"node_id": "A", "outstanding_task_ids": ["gs-1/A:tool:0:1"], "notifying_results": []},
        ],
    }

    outcome = repark_graph_outcome(None, session, repark)

    assert outcome.success is True
    assert outcome.drop_lease is True
    assert outcome.park is not None
    assert outcome.park.parked_event_key == "tool_wait:gs-1:0:A"
    assert outcome.park.parked_event_keys == ["tool_wait:gs-1:0:A"]
    parked_state = ToolWaitParkedState.from_jsonable(outcome.park.parked_state)
    # the park exception carries SCOPED ids; the blob names the rows by the session-qualified form (S1b)
    assert parked_state.outstanding_task_ids == ["gs-1/A:tool:0:1"]
    assert parked_state.graph_checkpoint == repark.graph_checkpoint


def test_multi_node_pending_produces_multi_event_keys() -> None:
    """Two co-pending nodes each get their OWN wake key in
    parked_event_keys - proves the multi-event pure-park shape survives
    a repark, not just the original park."""
    session = _session()
    repark = ToolWaitPark(
        outstanding_task_ids=["A:tool:0:1", "B:tool:0:1"],
        event_key="tool_wait:A:tool:0:1",
        notifying_results=[],
    )
    repark.graph_checkpoint = {
        "pending_tool_waits": [
            {"node_id": "A", "outstanding_task_ids": ["A:tool:0:1"], "notifying_results": []},
            {"node_id": "B", "outstanding_task_ids": ["B:tool:0:1"], "notifying_results": []},
        ],
    }

    outcome = repark_graph_outcome(None, session, repark)

    assert outcome.park.parked_event_keys == [
        "tool_wait:gs-1:0:A", "tool_wait:gs-1:0:B",
    ]


def test_node_tool_call_seq_is_threaded_through() -> None:
    session = _session()
    repark = ToolWaitPark(
        outstanding_task_ids=["A:tool:0:1"], event_key="tool_wait:A:tool:0:1",
        notifying_results=[],
    )
    repark.graph_checkpoint = {
        "pending_tool_waits": [
            {"node_id": "A", "outstanding_task_ids": ["A:tool:0:1"], "notifying_results": []},
        ],
    }

    outcome = repark_graph_outcome(None, session, repark, node_tool_call_seq={"A": 3})

    parked_state = ToolWaitParkedState.from_jsonable(outcome.park.parked_state)
    assert parked_state.node_tool_call_seq == {"A": 3}


def test_notifying_results_included_in_task_ids() -> None:
    session = _session()
    result = ToolResultPart(id="A:tool:0:2", output="inline", error=False)
    repark = ToolWaitPark(
        outstanding_task_ids=["A:tool:0:1"], event_key="tool_wait:A:tool:0:1",
        notifying_results=[("A:tool:0:2", result)],
    )
    repark.graph_checkpoint = {
        "pending_tool_waits": [
            {
                "node_id": "A", "outstanding_task_ids": ["gs-1/A:tool:0:1"],
                "notifying_results": [
                    ("gs-1/A:tool:0:2", {"id": "A:tool:0:2", "output": "inline", "error": False}),
                ],
            },
        ],
    }

    outcome = repark_graph_outcome(None, session, repark)

    parked_state = ToolWaitParkedState.from_jsonable(outcome.park.parked_state)
    assert parked_state.notifying_task_ids == ["gs-1/A:tool:0:2"]


def test_dispatches_to_yield_builder_for_yieldtoworker() -> None:
    """The dispatcher's OTHER branch - a co-pending human gate still
    produces the classic ParkedState/Yielded shape, not the tool_wait
    one, proving isinstance() picks the right builder both ways."""
    from primer.model.yield_ import Yielded, YieldToWorker
    from primer.worker.yield_runtime import ParkedState

    session = _session()
    repark = YieldToWorker(
        Yielded(tool_name="_approval", event_key="ask_user:B:tc-b"),
        tool_call_id="tc-b",
    )
    repark.graph_checkpoint = {"pending_agent_yields": [{"node_id": "B", "tool_call_id": "tc-b"}]}

    outcome = repark_graph_outcome(None, session, repark)

    assert outcome.park.parked_event_key == "ask_user:B:tc-b"
    parked_state = ParkedState.from_jsonable(outcome.park.parked_state)
    assert parked_state.tool_call_id == "tc-b"


def test_the_flat_lists_keep_the_form_each_entry_stores_a_legacy_park_stays_findable() -> None:
    """A park written before ids were qualified (a development flag-on park) has its rows under the BARE id. A partial
    wake re-parks node A's entry as it is stored; blindly qualifying the flat lists made the blob name rows that do not
    exist, and the next wake ended the session failed. Two entries, one bare (carried over from the legacy park) and
    one qualified: each keeps its own form, in entry order; the wake keys do not depend on the form."""
    session = _session()
    repark = ToolWaitPark(
        outstanding_task_ids=["A:tool:0:1", "B:tool:0:1"], event_key="tool_wait:A:tool:0:1",
        notifying_results=[("B:tool:0:2", ToolResultPart(id="B:tool:0:2", output="inline", error=False))],
    )
    repark.graph_checkpoint = {
        "pending_tool_waits": [
            {"node_id": "A", "outstanding_task_ids": ["A:tool:0:1"], "notifying_results": []},   # legacy, bare
            {
                "node_id": "B", "outstanding_task_ids": ["gs-1/B:tool:0:1"],
                "notifying_results": [
                    ("gs-1/B:tool:0:2", {"id": "B:tool:0:2", "output": "inline", "error": False}),
                ],
            },
        ],
    }

    outcome = repark_graph_outcome(None, session, repark)

    parked_state = ToolWaitParkedState.from_jsonable(outcome.park.parked_state)
    assert parked_state.outstanding_task_ids == ["A:tool:0:1", "gs-1/B:tool:0:1"]
    assert parked_state.notifying_task_ids == ["gs-1/B:tool:0:2"]
    assert outcome.park.parked_event_keys == ["tool_wait:gs-1:0:A", "tool_wait:gs-1:0:B"]


def test_with_no_entries_the_exceptions_scoped_ids_are_qualified() -> None:
    session = _session()
    repark = ToolWaitPark(
        outstanding_task_ids=["A:tool:0:1"], event_key="tool_wait:A:tool:0:1", notifying_results=[],
    )
    repark.graph_checkpoint = {"pending_tool_waits": []}

    outcome = repark_graph_outcome(None, session, repark)

    parked_state = ToolWaitParkedState.from_jsonable(outcome.park.parked_state)
    assert parked_state.outstanding_task_ids == ["gs-1/A:tool:0:1"]


def _malformed_repark(*entries: tuple[str, list[str]]) -> ToolWaitPark:
    repark = ToolWaitPark(
        outstanding_task_ids=[i for _, ids in entries for i in ids], event_key="tool_wait:obs", notifying_results=[],
    )
    repark.graph_checkpoint = {
        "pending_tool_waits": [
            {"node_id": node, "outstanding_task_ids": ids, "notifying_results": []} for node, ids in entries
        ],
    }
    return repark


def test_the_wake_keys_take_the_turn_of_each_batchs_ids_not_the_sessions_turn() -> None:
    """A carried-over batch keeps the key it was parked under after the session's turn moved on: the key is a pure
    function of the batch's ids (mutation N27, call-site leg: key on ``session.turn_no``)."""
    outcome = repark_graph_outcome(
        None, _session(turn_no=5), _malformed_repark(("a:b", ["gs-1/a:b:tool:0:1"]), ("C", ["gs-1/C:tool:5:1"])),
    )

    assert outcome.park.parked_event_keys == ["tool_wait:gs-1:0:a:b", "tool_wait:gs-1:5:C"]
    assert outcome.park.parked_event_key == "tool_wait:gs-1:0:a:b"


def test_a_malformed_batch_drops_only_its_own_key_and_the_park_is_still_written(caplog) -> None:
    import logging

    import primer.observability.metrics as metrics

    metrics.reset_for_test()
    with caplog.at_level(logging.ERROR):
        outcome = repark_graph_outcome(
            None, _session(), _malformed_repark(("A", ["gs-1/A:tool:03:1"]), ("B", ["gs-1/B:tool:0:1"])),
        )

    assert outcome.park is not None
    assert outcome.park.parked_event_keys == ["tool_wait:gs-1:0:B"]
    assert outcome.park.parked_event_key == "tool_wait:gs-1:0:B", "the park must be keyed on a batch that parses"
    parked_state = ToolWaitParkedState.from_jsonable(outcome.park.parked_state)
    assert parked_state.outstanding_task_ids == ["gs-1/A:tool:03:1", "gs-1/B:tool:0:1"], "the blob keeps both batches"
    assert metrics.tool_wait_malformed_scoped_id_total.labels("repark")._value.get() == 1.0
    assert any(r.levelno == logging.ERROR and "'gs-1/A:tool:03:1'" in r.getMessage() for r in caplog.records)


def test_a_repark_whose_every_batch_is_malformed_raises_instead_of_parking_with_no_wake_key() -> None:
    """A park with no wake key has no timeout backstop either: it is never written. The builder raises the turn
    invariant, and the resume ends the session failed (below)."""
    import pytest

    from primer.session.persistence import TurnInvariantError

    with pytest.raises(TurnInvariantError, match="no wake key"):
        repark_graph_outcome(
            None, _session(), _malformed_repark(("A", ["gs-1/A:tool:03:1"]), ("B", ["gs-1/B:tool:0:0"])),
        )


class _ResumePool:
    """The pool surface the two graph resume coordinators touch, with the REAL re-park builder."""

    def __init__(self) -> None:
        self._storage = None
        self._event_bus = None
        self.end_session_calls: list[str] = []

    async def _load_workspace_for_persist(self, workspace_id):
        return None

    async def _build_graph_executor(self, session, workspace):
        return object()

    async def _end_session(self, session, *, reason: str):
        self.end_session_calls.append(reason)
        return f"ENDED:{reason}"

    def _repark_graph_outcome(self, session, repark, *, node_tool_call_seq=None):
        return repark_graph_outcome(self, session, repark, node_tool_call_seq=node_tool_call_seq)

    def _graph_nested_agent_yield(self, checkpoint, tcid, event_key=None):
        return None

    async def _graph_agent_tool_result(self, checkpoint, tcid, payload, *, session_id, event_key=None):
        return None

    def _graph_value_yield_toolcall(self, checkpoint, tcid, event_key=None) -> bool:
        return False

    async def _write_approval_record_for_graph(self, **kwargs) -> None:
        return None


def _drain_returning(repark):
    async def _resume_graph_from_checkpoint(**kwargs):
        return "approved", repark, {}
    return _resume_graph_from_checkpoint


async def test_the_tool_wait_resume_ends_the_session_failed_when_its_repark_has_no_wake_key(monkeypatch) -> None:
    import primer.worker.graph_resume as graph_resume
    import primer.worker.tool_wait_resume_coordinator as coordinator

    async def _ready(task_storage, pending):
        return {"A": object()}, {}

    async def _no_records(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(coordinator, "resolve_ready_graph_tool_waits", _ready)
    monkeypatch.setattr(coordinator, "persist_resume_tool_result_records", _no_records)
    monkeypatch.setattr(
        graph_resume, "resume_graph_from_checkpoint", _drain_returning(_malformed_repark(("B", ["gs-1/B:tool:03:1"]))),
    )
    pool = _ResumePool()
    pool._storage = type("_SP", (), {"get_storage": lambda self, model: None})()
    parked = ToolWaitParkedState(
        outstanding_task_ids=["gs-1/A:tool:0:1"], notifying_task_ids=[], event_key="tool_wait:gs-1:0:A",
        llm_messages=[], turn_no=0, started_at=datetime.now(timezone.utc),
        graph_checkpoint={
            "pending_tool_waits": [{"node_id": "A", "outstanding_task_ids": ["gs-1/A:tool:0:1"], "notifying_results": []}],
        },
    )

    outcome = await coordinator.resume_graph_tool_wait(pool, _session(), parked)

    assert outcome == "ENDED:failed"
    assert pool.end_session_calls == ["failed"]


async def test_the_gate_resume_ends_the_session_failed_when_its_repark_has_no_wake_key(monkeypatch) -> None:
    import primer.worker.graph_resume as graph_resume
    from primer.model.yield_ import Yielded
    from primer.worker.graph_resume_coordinator import resume_graph_engine
    from primer.worker.yield_runtime import ParkedState

    monkeypatch.setattr(
        graph_resume, "resume_graph_from_checkpoint", _drain_returning(_malformed_repark(("B", ["gs-1/B:tool:03:1"]))),
    )
    now = datetime.now(timezone.utc)
    session = _session().model_copy(update={
        "parked_at": now, "parked_state": {"resume_event_key": "tool_approval:gs-1:tc-1"},
    })
    parked = ParkedState(
        yielded=Yielded(tool_name="_approval", event_key="tool_approval:gs-1:tc-1"),
        llm_messages=[], turn_no=0, started_at=now, tool_call_id="tc-1",
        resume_event_payload={"decision": "approved"},
        graph_checkpoint={"pending_toolcalls": [{"node_id": "A", "tool_call_id": "tc-1"}]},
    )
    pool = _ResumePool()

    outcome = await resume_graph_engine(pool, session, parked)

    assert outcome == "ENDED:failed"
    assert pool.end_session_calls == ["failed"]
