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

    async def upsert(self, kind, entity_id: str, **kwargs) -> None:
        self.upserted.append((kind, entity_id))


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
    row = await task_storage.get("x:tool:0:1")
    assert row is not None
    assert row.state == ToolCallTaskState.QUEUED
    assert row.batch_task_ids == ["x:tool:0:1"]
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
    assert deps.claim_engine.upserted == [(ClaimKind.TOOL_CALL, "x:tool:0:1")]


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
    row = await task_storage.get("x:tool:0:1")
    assert row is not None
    assert row.state == ToolCallTaskState.QUEUED
    assert deps.claim_engine.upserted == [(ClaimKind.TOOL_CALL, "x:tool:0:1")]

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
