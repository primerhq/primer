"""A user Cancel's hard preempt, through the real pool and the real dispatch (console review 2026-10-08, C-011).

``tests/session/test_hard_cancel_lands_the_cancelled_exit.py`` pins the dispatch half with the task cancelled by hand. This runs
``WorkerPool._run_engine`` -> ``_run_engine_session`` -> the real ``run_one_session_turn`` over a real workspace, and delivers the
cancel the way ``_cancel_loop`` / the row reconciler do (``scope.cancel_once``), so what the POOL does with the outcome is under
test too: the lease is released as a success, the pool's own convergence (``_end_session``) does not run a second time, and the
terminal event and the cancelled metric happen exactly once.

The exit returns its outcome instead of re-raising the ``CancelledError``, on purpose: ``_finish_despite_cancel`` documents (and
``tests/session/test_dispatch_interrupt.py`` pins) that a re-raise would drop the outcome, the pool's convergence would skip a row
that is already ENDED, and ``on_release`` would write a terminal ERROR record after a clean exit.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

import primer.observability.metrics as metrics
import primer.session.dispatch as dispatch
from primer.bus.in_memory import InMemoryEventBus
from primer.int.claim import ClaimKind
from primer.model.chat import TextDelta
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import AgentSessionBinding, SessionMessageKind, SessionStatus, WorkspaceSession
from primer.worker.pool import WorkerPool
from tests._support.off_golden import open_session
from tests.conftest import _FakeStorageProvider
from tests.worker.test_preempt_cancel_converge import _build_engine, _claim_session

WORKSPACE_ID = "w1"


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


class _Registry:
    def __init__(self, workspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id: str):
        return self._workspace if workspace_id == WORKSPACE_ID else None


class _BlocksInTheModelCall:
    """Streams some text, then waits for a model that does not answer; only a hard cancel ends it."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()

    async def invoke(self, messages: list[Any], **kwargs: Any):
        yield TextDelta(text="working on it", index=0)
        self.reached.set()
        await asyncio.Event().wait()


async def _run_a_cancelled_turn(tmp_path, *, during_the_landing=None):
    """Run one turn under the real pool until it blocks, flag the Cancel on the row, hard-cancel the scope once, and wait for the
    pool to finish. Returns what the assertions need."""
    backend, workspace, workspace_session = await open_session(tmp_path)
    try:
        sid = workspace_session.session_id
        storage_provider = _FakeStorageProvider()
        sessions = storage_provider.get_storage(WorkspaceSession)
        await sessions.create(WorkspaceSession(
            id=sid, workspace_id=WORKSPACE_ID, binding=AgentSessionBinding(agent_id="ag-1"),
            status=SessionStatus.RUNNING, created_at=datetime.now(UTC), turn_no=0,
        ))
        engine = _build_engine(sessions)
        bus = InMemoryEventBus()
        await bus.initialize()
        published: list[tuple[str, dict]] = []
        publish = bus.publish

        async def spy_publish(key: str, payload: dict) -> None:
            published.append((key, payload))
            await publish(key, payload)

        bus.publish = spy_publish
        pool = WorkerPool(
            config=WorkerConfig(concurrency=1), scheduler=None, storage=storage_provider,  # type: ignore[arg-type]
            workspace_registry=_Registry(workspace), provider_registry=None, engine=engine, event_bus=bus,  # type: ignore[arg-type]
        )
        pool._worker_id = "wrk-preempt"
        executor = _BlocksInTheModelCall()

        async def build_session_executor(_session):
            return executor

        pool._build_session_executor = build_session_executor
        lease = await _claim_session(engine, sid)
        task = asyncio.create_task(pool._run_engine(lease, pool._run_engine_session))
        await asyncio.wait_for(executor.reached.wait(), timeout=5.0)

        key = (lease.kind, lease.entity_id)
        scope = pool._active_scopes.get(key)
        assert scope is not None, "the cancel scope must be registered while the turn is in flight"
        if during_the_landing is not None:
            during_the_landing(scope)
        row = await sessions.get(sid)
        row.cancel_requested = True
        await sessions.update(row)
        assert scope.cancel_once("user_signal"), "the first cancel is delivered"
        await asyncio.wait_for(task, timeout=15.0)

        log = workspace.root / workspace.template.state_path / "sessions" / sid / "messages.jsonl"
        records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if '"kind"' in line]
        released = await engine.claim_due("wrk-preempt", max_count=10)
        await bus.aclose()
        return {
            "sid": sid, "row": await sessions.get(sid), "records": records, "published": published, "scope": scope,
            "reclaimable": any(x.kind == ClaimKind.SESSION and x.entity_id == sid for x in released),
            "ref": dispatch._binding_ref(await sessions.get(sid)), "pool": pool, "key": key,
        }
    finally:
        await workspace_session.aclose()
        await backend.aclose()


def _terminal_events(published, sid: str) -> list[dict]:
    return [payload for key, payload in published if key == f"session:{sid}:terminal"]


async def test_a_hard_cancelled_slow_turn_lands_once_and_the_pool_releases_it_cleanly(tmp_path) -> None:
    run = await _run_a_cancelled_turn(tmp_path)

    row = run["row"]
    assert row.status == SessionStatus.ENDED and row.ended_reason == "cancelled" and row.ended_at is not None
    kinds = [r["kind"] for r in run["records"]]
    assert kinds[-1] == SessionMessageKind.CANCELLED, f"the transcript must end in CANCELLED, got {kinds}"
    assert kinds.count(SessionMessageKind.CANCELLED) == 1, "one CANCELLED record, not one from dispatch and one from the pool"
    assert SessionMessageKind.ERROR not in kinds, "a terminal ERROR record after a clean exit is what a re-raise would cause"
    assert run["records"][-1]["payload"]["reason"] == "operator_cancel"
    assert _terminal_events(run["published"], run["sid"]) == [{"status": "ended", "ended_reason": "cancelled"}], (
        "exactly one terminal event"
    )
    assert metrics.turns_total.labels(run["ref"], "cancelled")._value.get() == 1.0, "exactly one cancelled-metric increment"
    assert metrics.turns_total.labels(run["ref"], "failed")._value.get() == 0.0
    assert not run["reclaimable"], "the ended session must not keep a lease"
    assert run["key"] not in run["pool"]._active_scopes


async def test_a_second_hard_cancel_during_the_landing_does_not_cut_it(tmp_path) -> None:
    """The reconciler and the NOTIFY can both report the Cancel, and the heartbeat's lost-lease verdict can follow: the cancel
    ``cancel_once`` refuses is still delivered by the unconditional ``cancel``. The landing must absorb it (it runs as its own
    task) and finish: the transition, the terminal event and the metric all happen, once."""
    landed = {"second_cancel_delivered": 0}
    real_transition = dispatch._transition_session_status

    def second_cancel_arrives_while_the_exit_writes(scope) -> None:
        async def transition_with_a_second_cancel(*args, **kwargs):
            scope.cancel("preempted")                       # the unconditional one, delivered mid-landing
            landed["second_cancel_delivered"] += 1
            await asyncio.sleep(0.05)
            return await real_transition(*args, **kwargs)

        dispatch._transition_session_status = transition_with_a_second_cancel

    try:
        run = await _run_a_cancelled_turn(tmp_path, during_the_landing=second_cancel_arrives_while_the_exit_writes)
    finally:
        dispatch._transition_session_status = real_transition

    assert landed["second_cancel_delivered"] == 1, "the second cancel never arrived inside the landing"
    row = run["row"]
    assert row.status == SessionStatus.ENDED and row.ended_reason == "cancelled"
    assert [r["kind"] for r in run["records"]][-1] == SessionMessageKind.CANCELLED
    assert _terminal_events(run["published"], run["sid"]) == [{"status": "ended", "ended_reason": "cancelled"}]
    assert metrics.turns_total.labels(run["ref"], "cancelled")._value.get() == 1.0
    assert not run["reclaimable"]
