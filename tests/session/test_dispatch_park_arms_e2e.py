"""``run_one_session_turn``-level tests for dispatch.py's two
graph-checkpoint-bearing park arms (Phase 3 stage 7a, 01a0518b, 7a gate
verdict item 7).

Both arms had ONLY unit-level coverage of their own helper
(``_materialize_pending_tool_wait_rows``) or synthetic direct-exception
tests - nothing drove them through the REAL ``run_one_session_turn``
turn loop, so a regression in the arm's OWN wiring (e.g. deleting the
mixed arm's ``_materialize_pending_tool_wait_rows`` call, or the
``combined_event_keys`` fold) would leave the suite green while every
mixed park silently stopped registering its co-pending tool_wait
batch's claim.

* the MIXED arm (``except YieldToWorker`` - a human gate co-pending with
  a tool_wait batch in the SAME graph_checkpoint);
* the PURE-GRAPH arm (``except ToolWaitPark`` with a non-None
  ``graph_checkpoint`` - no human gate at all).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.chat import Message, TextPart, ToolCallEnd, ToolCallStart
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import ToolWaitPark, Yielded, YieldToWorker
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn

from tests.conftest import _FakeStorageProvider


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeWorkspaceIO:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = {}

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        key = (session_id, "messages.jsonl")
        self._data[key] = self._data.get(key, b"") + line


class _FakeEventBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, key: str, payload: dict) -> None:
        self.published.append((key, payload))


class _RecordingClaimEngine:
    def __init__(self) -> None:
        self.upserted: list[tuple] = []
        self.priorities: dict[tuple, object] = {}

    async def upsert(self, kind, entity_id: str, **kwargs) -> None:
        self.upserted.append((kind, entity_id))
        self.priorities[(kind, entity_id)] = kwargs.get("priority")


def _session(session_id: str) -> WorkspaceSession:
    return WorkspaceSession(
        id=session_id,
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
    )


@pytest.mark.asyncio
async def test_pure_graph_arm_materializes_per_node_rows_through_real_turn_loop() -> None:
    """except ToolWaitPark with graph_checkpoint set - the PURE-graph
    arm. Drives _materialize_pending_tool_wait_rows via the REAL turn
    loop rather than a direct unit call."""
    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    task_storage = storage_provider.get_storage(ToolCallTask)

    session = _session("s-pure-graph")
    await session_storage.create(session)

    class _PureGraphExecutor:
        # ToolWaitPark can only ever be raised with the flag on
        # (run_agent_turn's own routing gate) - dispatch.py's TOOL_CALL
        # record-stash/eager-flush is gated on it (7a gate review item
        # A), so a fake standing in for this exact scenario must say so.
        _tool_calls_as_claims_enabled = True

        async def invoke(self, messages, **kwargs):
            yield ToolCallStart(id="call_a", name="tool_a", index=0)
            yield ToolCallEnd(id="call_a", arguments={"x": 1}, index=0)
            park = ToolWaitPark(
                outstanding_task_ids=["x:tool:0:1"],
                event_key="tool_wait:x:tool:0:1",
            )
            park.graph_checkpoint = {
                "pending_tool_waits": [
                    {
                        "node_id": "x",
                        "outstanding_task_ids": ["x:tool:0:1"],
                        "notifying_results": [],
                    },
                ],
            }
            raise park
            yield  # pragma: no cover - unreachable, keeps this a generator

    async def _build_executor(_session: WorkspaceSession):
        return _PureGraphExecutor()

    deps = SessionDispatchDeps(
        storage_provider=storage_provider,
        workspace_io=_FakeWorkspaceIO(),
        event_bus=_FakeEventBus(),
        build_executor=_build_executor,
        claim_engine=_RecordingClaimEngine(),
    )

    from primer.int.claim import ClaimKind, Lease

    lease = Lease(
        kind=ClaimKind.SESSION, entity_id=session.id, claimed_by="worker-1",
        claimed_at=_now(), expires_at=_now(), attempt_count=0, last_error=None,
    )
    outcome = await run_one_session_turn(lease, deps)

    assert outcome.success is True
    assert outcome.park is not None
    # Row created via the graph_checkpoint branch (per-node breakdown),
    # NOT the flat-field loop - if the flat loop ran instead it would
    # use the SAME scoped id here too, so this alone doesn't
    # discriminate.
    # (S1b: the row id is the session-qualified form of the scoped id.)
    row = await task_storage.get("s-pure-graph/x:tool:0:1")
    assert row is not None
    assert row.state == ToolCallTaskState.QUEUED
    assert row.batch_task_ids == ["s-pure-graph/x:tool:0:1"]
    assert row.scoped_call_id == "x:tool:0:1"
    # 7a gate review (verdict R2-6): the SINGLE parked_event_key below is
    # computed identically regardless of which branch ran (a pure
    # function of session_id/turn_no/scoped-id, evaluated BEFORE the
    # graph_checkpoint/flat-field branch split) - it does NOT
    # discriminate between them, despite what this comment used to
    # claim. The MULTI-event parked_event_keys list DOES: only the
    # graph_checkpoint branch computes per-node wake keys via
    # materialize_pending_tool_wait_rows (the flat-field loop leaves
    # per_node_wake_keys empty, folding parked_event_keys to None) -
    # assert its actual per-node content.
    assert outcome.park.parked_event_key == "tool_wait:s-pure-graph:0:x"
    assert outcome.park.parked_event_keys == ["tool_wait:s-pure-graph:0:x"]
    assert deps.claim_engine.upserted == [(ClaimKind.TOOL_CALL, "s-pure-graph/x:tool:0:1")]
    assert deps.claim_engine.priorities[(ClaimKind.TOOL_CALL, "s-pure-graph/x:tool:0:1")] == 50  # CLAIM_PRIORITY_RESUME
    # the parked blob names the rows by the qualified id too
    assert outcome.park.parked_state["outstanding_task_ids"] == ["s-pure-graph/x:tool:0:1"]
    assert outcome.park.parked_state["graph_checkpoint"]["pending_tool_waits"][0]["outstanding_task_ids"] == [
        "s-pure-graph/x:tool:0:1"
    ]


@pytest.mark.asyncio
async def test_mixed_arm_materializes_co_pending_tool_wait_through_real_turn_loop() -> None:
    """except YieldToWorker (a human ask_user gate) with a CO-PENDING
    tool_wait batch riding in the SAME graph_checkpoint - the MIXED arm.
    Deleting _materialize_pending_tool_wait_rows' call from this branch
    would strand every mixed park's tool_wait batch with the suite
    green (nothing else in this arm's OWN code path exercises it) -
    this test drives that exact call through run_one_session_turn."""
    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    task_storage = storage_provider.get_storage(ToolCallTask)

    session = _session("s-mixed")
    await session_storage.create(session)

    class _MixedParkExecutor:
        # A co-pending tool_wait batch in graph_checkpoint can only ever
        # exist with the flag on (same reasoning as _PureGraphExecutor's
        # own comment above).
        _tool_calls_as_claims_enabled = True

        async def invoke(self, messages, **kwargs):
            yield ToolCallStart(id="call_a", name="tool_a", index=0)
            yield ToolCallEnd(id="call_a", arguments={"x": 1}, index=0)
            yield ToolCallStart(id="call_gate", name="ask_user", index=1)
            yield ToolCallEnd(id="call_gate", arguments={}, index=1)
            yld = YieldToWorker(
                Yielded(
                    tool_name="ask_user", event_key="ask_user:s-mixed:call_gate",
                    # 7a gate review (verdict R2-7): a gate with its OWN
                    # multi-event keys (not just the single primary one),
                    # so combined_event_keys' union with the tool_wait
                    # batch's wake key below is exercised for real -
                    # membership alone ("the tool_wait key is IN the
                    # list somewhere") doesn't prove the gate's own keys
                    # survived the fold too.
                    event_keys=[
                        "ask_user:s-mixed:call_gate",
                        "ask_user:s-mixed:call_gate:alt",
                    ],
                    resume_metadata={"prompt": "color?"},
                ),
                tool_call_id="call_gate",
                llm_messages=[
                    Message(role="assistant", parts=[TextPart(text="let me check")]),
                ],
            )
            yld.graph_checkpoint = {
                "pending_tool_waits": [
                    {
                        "node_id": "x",
                        "outstanding_task_ids": ["x:tool:0:1"],
                        "notifying_results": [],
                    },
                ],
            }
            raise yld
            yield  # pragma: no cover - unreachable, keeps this a generator

    async def _build_executor(_session: WorkspaceSession):
        return _MixedParkExecutor()

    deps = SessionDispatchDeps(
        storage_provider=storage_provider,
        workspace_io=_FakeWorkspaceIO(),
        event_bus=_FakeEventBus(),
        build_executor=_build_executor,
        claim_engine=_RecordingClaimEngine(),
    )

    from primer.int.claim import ClaimKind, Lease

    lease = Lease(
        kind=ClaimKind.SESSION, entity_id=session.id, claimed_by="worker-1",
        claimed_at=_now(), expires_at=_now(), attempt_count=0, last_error=None,
    )
    outcome = await run_one_session_turn(lease, deps)

    assert outcome.success is True
    assert outcome.park is not None
    # The co-pending tool_wait batch's row exists - proof
    # _materialize_pending_tool_wait_rows actually ran in THIS arm, not
    # just the pure one.
    row = await task_storage.get("s-mixed/x:tool:0:1")
    assert row is not None
    assert row.state == ToolCallTaskState.QUEUED
    assert deps.claim_engine.upserted == [(ClaimKind.TOOL_CALL, "s-mixed/x:tool:0:1")]
    assert deps.claim_engine.priorities[(ClaimKind.TOOL_CALL, "s-mixed/x:tool:0:1")] == 50  # CLAIM_PRIORITY_RESUME

    # The gate's OWN key is still the primary parked_event_key (a human
    # gate always addresses the park); the tool_wait batch's wake key is
    # folded into the combined multi-event list alongside it.
    assert outcome.park.parked_event_key == "ask_user:s-mixed:call_gate"
    # 7a gate review (verdict R2-7): exact UNION content, not membership
    # - proves the gate's OWN multi-event keys survive the fold intact
    # (in their original order) alongside the tool_wait batch's wake
    # key, matching dispatch.py's actual construction
    # (``combined_event_keys = list(yielded.event_keys or []);
    # combined_event_keys.extend(extra_wake_keys)``).
    assert outcome.park.parked_event_keys == [
        "ask_user:s-mixed:call_gate",
        "ask_user:s-mixed:call_gate:alt",
        "tool_wait:s-mixed:0:x",
    ]

    parked_state = outcome.park.parked_state
    assert (
        parked_state["graph_checkpoint"]["pending_tool_waits"][0]["node_id"] == "x"
    )


@pytest.mark.asyncio
async def test_two_graph_sessions_parking_the_same_scoped_batch_keep_separate_rows_blobs_and_leases() -> None:
    """S1b, the GRAPH surface: every graph session mints `<node>:tool:0:1` for its first call, so two sessions parked
    on the same storage and claim engine used to share one row and one lease (red on main: the second park adopts the
    first's row). Each must own its row, its lease and a parked blob that names exactly its own."""
    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    task_storage = storage_provider.get_storage(ToolCallTask)
    claim_engine = _RecordingClaimEngine()

    class _GraphExecutor:
        _tool_calls_as_claims_enabled = True

        async def invoke(self, messages, **kwargs):
            yield ToolCallStart(id="call_a", name="tool_a", index=0)
            yield ToolCallEnd(id="call_a", arguments={"x": 1}, index=0)
            park = ToolWaitPark(
                outstanding_task_ids=["x:tool:0:1"], event_key="tool_wait:x:tool:0:1",
                call_ids={"x:tool:0:1": "call_a"},
            )
            park.graph_checkpoint = {
                "pending_tool_waits": [
                    {
                        "node_id": "x", "outstanding_task_ids": ["x:tool:0:1"], "notifying_results": [],
                        "call_ids": {"x:tool:0:1": "call_a"},
                    },
                ],
            }
            raise park
            yield  # pragma: no cover - unreachable, keeps this a generator

    async def _build_executor(_session: WorkspaceSession):
        return _GraphExecutor()

    from primer.int.claim import ClaimKind, Lease

    outcomes = {}
    for session_id in ("s-graph-A", "s-graph-B"):
        await session_storage.create(_session(session_id))
        deps = SessionDispatchDeps(
            storage_provider=storage_provider, workspace_io=_FakeWorkspaceIO(), event_bus=_FakeEventBus(),
            build_executor=_build_executor, claim_engine=claim_engine,
        )
        lease = Lease(
            kind=ClaimKind.SESSION, entity_id=session_id, claimed_by="worker-1",
            claimed_at=_now(), expires_at=_now(), attempt_count=0, last_error=None,
        )
        outcomes[session_id] = await run_one_session_turn(lease, deps)

    assert sorted(entity for _, entity in claim_engine.upserted) == [
        "s-graph-A/x:tool:0:1", "s-graph-B/x:tool:0:1",
    ]
    for session_id, outcome in outcomes.items():
        row = await task_storage.get(f"{session_id}/x:tool:0:1")
        assert row is not None and row.session_id == session_id and row.call_id == "call_a"
        assert row.batch_task_ids == [f"{session_id}/x:tool:0:1"]
        blob = outcome.park.parked_state
        assert blob["outstanding_task_ids"] == [f"{session_id}/x:tool:0:1"]
        assert blob["graph_checkpoint"]["pending_tool_waits"][0]["outstanding_task_ids"] == [
            f"{session_id}/x:tool:0:1"
        ]
        assert outcome.park.parked_event_key == f"tool_wait:{session_id}:0:x"


# ---------------------------------------------------------------------------
# A malformed scoped id at the park arms: the batch's key is dropped (ERROR plus a counter), and a park is never
# written with no wake key at all (it would have no timeout backstop either). The one mint site never produces a
# malformed id, so a hand-made one is given a TOOL_CALL record here to stand in for that bug.
# ---------------------------------------------------------------------------


def _seed_tool_call_records(monkeypatch, *scoped_ids: str) -> None:
    import primer.session.dispatch as dispatch
    from primer.session.persistence import _CoalesceState

    def _seeded() -> _CoalesceState:
        state = _CoalesceState()
        for scoped_id in scoped_ids:
            state.tool_call_record_seq[scoped_id] = 1
            state.tool_call_record_name[scoped_id] = "tool_a"
        return state

    monkeypatch.setattr(dispatch, "_CoalesceState", _seeded)


async def _run_turn(session_id: str, park: Exception):
    from primer.int.claim import ClaimKind, Lease

    storage_provider = _FakeStorageProvider()
    await storage_provider.get_storage(WorkspaceSession).create(_session(session_id))

    class _Executor:
        _tool_calls_as_claims_enabled = True

        async def invoke(self, messages, **kwargs):
            raise park
            yield  # pragma: no cover - unreachable, keeps this a generator

    async def _build_executor(_session: WorkspaceSession):
        return _Executor()

    io = _FakeWorkspaceIO()
    deps = SessionDispatchDeps(
        storage_provider=storage_provider, workspace_io=io, event_bus=_FakeEventBus(),
        build_executor=_build_executor, claim_engine=_RecordingClaimEngine(),
    )
    lease = Lease(
        kind=ClaimKind.SESSION, entity_id=session_id, claimed_by="worker-1",
        claimed_at=_now(), expires_at=_now(), attempt_count=0, last_error=None,
    )
    outcome = await run_one_session_turn(lease, deps)
    return outcome, storage_provider, io


def _graph_tool_wait_park(*entries: tuple[str, list[str]]) -> ToolWaitPark:
    park = ToolWaitPark(
        outstanding_task_ids=[i for _, ids in entries for i in ids], event_key="tool_wait:obs",
    )
    park.graph_checkpoint = {
        "pending_tool_waits": [
            {"node_id": node, "outstanding_task_ids": list(ids), "notifying_results": []} for node, ids in entries
        ],
    }
    return park


def _messages(io: _FakeWorkspaceIO, session_id: str) -> list[dict]:
    import json

    raw = io._data.get((session_id, "messages.jsonl"), b"")
    return [json.loads(line) for line in raw.decode().splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_pure_graph_arm_keeps_the_batch_that_parses_and_parks(monkeypatch) -> None:
    import primer.observability.metrics as metrics

    metrics.reset_for_test()
    _seed_tool_call_records(monkeypatch, "B:tool:03:1", "x:tool:0:1")

    outcome, storage_provider, _ = await _run_turn(
        "s-one-bad", _graph_tool_wait_park(("B", ["B:tool:03:1"]), ("x", ["x:tool:0:1"])),
    )

    assert outcome.park is not None, "one malformed batch must not stop the park"
    assert outcome.park.parked_event_keys == ["tool_wait:s-one-bad:0:x"]
    assert outcome.park.parked_event_key == "tool_wait:s-one-bad:0:x", "the park is keyed on a batch that parses"
    tasks = storage_provider.get_storage(ToolCallTask)
    assert await tasks.get("s-one-bad/B:tool:03:1") is not None, "row creation is unchanged"
    assert metrics.tool_wait_malformed_scoped_id_total.labels("materializer")._value.get() == 1.0


@pytest.mark.asyncio
async def test_pure_graph_arm_with_no_parseable_batch_ends_the_turn_failed_instead_of_parking(monkeypatch) -> None:
    _seed_tool_call_records(monkeypatch, "B:tool:03:1")

    outcome, storage_provider, io = await _run_turn("s-all-bad", _graph_tool_wait_park(("B", ["B:tool:03:1"])))

    assert outcome.park is None, "a park with no wake key was written"
    assert outcome.success is False
    row = await storage_provider.get_storage(WorkspaceSession).get("s-all-bad")
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed")
    errors = [m for m in _messages(io, "s-all-bad") if m["kind"] == "error"]
    assert errors and "no wake key" in errors[-1]["payload"]["message"], errors


@pytest.mark.asyncio
async def test_agent_tool_wait_arm_with_a_malformed_id_ends_the_turn_failed_instead_of_parking(monkeypatch) -> None:
    """The key is computed after the rows (row creation is unchanged, as in the graph arm), so a record missing for
    an id still fails the turn with THAT reason first (test_tool_wait_seam_e2e pins it)."""
    import primer.observability.metrics as metrics

    metrics.reset_for_test()
    _seed_tool_call_records(monkeypatch, "x:tool:03:1")

    outcome, storage_provider, io = await _run_turn(
        "s-agent-bad", ToolWaitPark(outstanding_task_ids=["x:tool:03:1"], event_key="tool_wait:obs"),
    )

    assert outcome.park is None and outcome.success is False
    row = await storage_provider.get_storage(WorkspaceSession).get("s-agent-bad")
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed")
    # PINS A DECLARED RESIDUAL, not a wanted outcome: a malformed pure batch leaves its QUEUED rows and TOOL_CALL
    # upserts behind before the turn ends failed (the key is computed after the rows). The abandon work of plan
    # section 3.8 flips this; this assertion is expected to change then.
    assert await storage_provider.get_storage(ToolCallTask).get("s-agent-bad/x:tool:03:1") is not None
    assert metrics.tool_wait_malformed_scoped_id_total.labels("dispatch")._value.get() == 1.0
    errors = [m for m in _messages(io, "s-agent-bad") if m["kind"] == "error"]
    assert errors and "no wake key" in errors[-1]["payload"]["message"], errors


@pytest.mark.asyncio
async def test_mixed_arm_drops_a_malformed_batchs_key_and_parks_on_the_gate(monkeypatch) -> None:
    """The mixed park always has its human gate's key, so a malformed co-pending batch only loses its own."""
    import primer.observability.metrics as metrics

    metrics.reset_for_test()
    _seed_tool_call_records(monkeypatch, "B:tool:03:1")
    yld = YieldToWorker(
        Yielded(tool_name="ask_user", event_key="ask_user:s-mixed-bad:call_gate", resume_metadata={"prompt": "?"}),
        tool_call_id="call_gate",
    )
    yld.graph_checkpoint = _graph_tool_wait_park(("B", ["B:tool:03:1"])).graph_checkpoint

    outcome, storage_provider, _ = await _run_turn("s-mixed-bad", yld)

    assert outcome.park is not None
    assert outcome.park.parked_event_key == "ask_user:s-mixed-bad:call_gate"
    assert not any(k.startswith("tool_wait:") for k in outcome.park.parked_event_keys or [])
    assert await storage_provider.get_storage(ToolCallTask).get("s-mixed-bad/B:tool:03:1") is not None
    assert metrics.tool_wait_malformed_scoped_id_total.labels("materializer")._value.get() == 1.0
