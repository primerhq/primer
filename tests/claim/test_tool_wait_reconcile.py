from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
import primer.claim.tool_wait_reconcile as reconcile_module
from primer.claim.tool_wait_reconcile import (
    reconcile_terminal_tool_wait_parks,
    verify_and_mark_batch_resumable,
)
from primer.bus.scheduler_tasks import ToolWaitWakeReconciler
from primer.int.claim import ClaimKind, ParkRequest, PostReleaseWake, ReleaseOutcome
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.yields import tool_wait_event_key
from tests.conftest import _FakeStorageProvider, _InMemoryStorage


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Engine:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.lease = False
        self.marks = 0
        self.fail_once = fail_once

    async def mark_resumable(self, kind, entity_id, *, priority=50) -> None:
        assert kind is ClaimKind.SESSION
        self.marks += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("lost after row flip")
        self.lease = True

    async def has_lease(self, kind, entity_id) -> bool:
        assert kind is ClaimKind.SESSION
        return self.lease


class _RaceStorage(_InMemoryStorage[WorkspaceSession]):
    def __init__(self, update: dict) -> None:
        super().__init__(WorkspaceSession)
        self._update = update

    async def patch_if(self, id, patch=None, *, where, set_paths=None, conn=None):
        self._data[id] = self._data[id].model_copy(update=self._update)
        return await super().patch_if(
            id, patch, where=where, set_paths=set_paths, conn=conn,
        )


def _world(*, task_state=ToolCallTaskState.DONE):
    sid = "s1"
    task_id = f"{sid}/x:tool:0:1"
    key = tool_wait_event_key(sid, scoped_task_id=task_id)
    parked_at = _now()
    state = {
        "kind": "tool_wait",
        "outstanding_task_ids": [task_id],
        "notifying_task_ids": [],
    }
    session = WorkspaceSession(
        id=sid,
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="a1"),
        status=SessionStatus.RUNNING,
        created_at=parked_at,
        turn_no=0,
        parked_status="parked",
        parked_event_key=key,
        parked_at=parked_at,
        parked_state=state,
        parked_tool_batches=True,
    )
    task = ToolCallTask(
        id=task_id,
        session_id=sid,
        turn_no=0,
        tool_name="tool",
        state=task_state,
        record_seq=1,
        created_at=parked_at,
        batch_task_ids=[task_id],
    )
    sessions = _InMemoryStorage(WorkspaceSession)
    sessions._data[sid] = session
    tasks = _InMemoryStorage(ToolCallTask)
    tasks._data[task_id] = task
    signal = PostReleaseWake(
        session_id=sid,
        event_key=key,
        payload={"tool_wait_ready": True},
        required_terminal_task_ids=(task_id,),
    )
    return sessions, tasks, signal


@pytest.mark.asyncio
async def test_ready_batch_advances_once_and_redelivery_repairs_lease() -> None:
    sessions, tasks, signal = _world()
    engine = _Engine()
    assert await verify_and_mark_batch_resumable(
        session_id="s1", session_storage=sessions, task_storage=tasks,
        engine=engine, source="hook", signal=signal,
    ) == "advanced"
    first = await sessions.get("s1")
    assert first is not None and first.parked_status == "resumable"
    assert first.resumable_at is not None and engine.marks == 1
    stamp = first.resumable_at
    engine.lease = False
    assert await verify_and_mark_batch_resumable(
        session_id="s1", session_storage=sessions, task_storage=tasks,
        engine=engine, source="hook", signal=signal,
    ) == "lease_rearmed"
    assert (await sessions.get("s1")).resumable_at == stamp
    assert engine.marks == 2


@pytest.mark.asyncio
async def test_legacy_marker_null_hook_wakes_but_scans_refuse_it() -> None:
    sessions, tasks, signal = _world()
    row = await sessions.get("s1")
    sessions._data["s1"] = row.model_copy(update={"parked_tool_batches": None})
    engine = _Engine()
    assert await verify_and_mark_batch_resumable(
        session_id="s1", session_storage=sessions, task_storage=tasks,
        engine=engine, source="hook", signal=signal,
    ) == "advanced"
    assert (await sessions.get("s1")).parked_tool_batches is None
    engine.lease = False
    assert await verify_and_mark_batch_resumable(
        session_id="s1", session_storage=sessions, task_storage=tasks,
        engine=engine, source="hook", signal=signal,
    ) == "lease_rearmed"

    for source in ("boot", "periodic"):
        scan_sessions, scan_tasks, _ = _world()
        scan_row = await scan_sessions.get("s1")
        scan_sessions._data["s1"] = scan_row.model_copy(
            update={"parked_tool_batches": None},
        )
        scan_engine = _Engine()
        assert await verify_and_mark_batch_resumable(
            session_id="s1", session_storage=scan_sessions,
            task_storage=scan_tasks, engine=scan_engine, source=source,
        ) == "lifecycle"
        assert scan_engine.marks == 0


@pytest.mark.asyncio
async def test_nonterminal_or_corrupt_batch_never_wakes() -> None:
    sessions, tasks, signal = _world(task_state=ToolCallTaskState.QUEUED)
    engine = _Engine()
    assert await verify_and_mark_batch_resumable(
        session_id="s1", session_storage=sessions, task_storage=tasks,
        engine=engine, source="hook", signal=signal,
    ) == "not_ready"
    task = await tasks.get(signal.required_terminal_task_ids[0])
    tasks._data[task.id] = task.model_copy(update={"batch_task_ids": ["wrong"]})
    assert await verify_and_mark_batch_resumable(
        session_id="s1", session_storage=sessions, task_storage=tasks,
        engine=engine, source="hook", signal=signal,
    ) == "not_ready"
    assert engine.marks == 0


@pytest.mark.asyncio
async def test_graph_ready_batch_wakes_independently_of_other_and_human_leaf() -> None:
    sessions, tasks, signal = _world()
    row = await sessions.get("s1")
    second_id = "s1/B:tool:0:1"
    second_key = tool_wait_event_key("s1", scoped_task_id=second_id)
    human_key = "ask_user:s1:g1"
    sessions._data["s1"] = row.model_copy(update={
        "parked_event_key": signal.event_key,
        "parked_event_keys": [signal.event_key, second_key, human_key],
        "parked_state": {
            "graph_checkpoint": {"pending_tool_waits": [
                {"node_id": "x", "outstanding_task_ids": list(signal.required_terminal_task_ids), "notifying_results": []},
                {"node_id": "B", "outstanding_task_ids": [second_id], "notifying_results": []},
            ]},
        },
    })
    tasks._data[second_id] = ToolCallTask(
        id=second_id, session_id="s1", turn_no=0, tool_name="other",
        state=ToolCallTaskState.QUEUED, record_seq=2, created_at=_now(),
        batch_task_ids=[second_id],
    )
    engine = _Engine()
    assert await verify_and_mark_batch_resumable(
        session_id="s1", session_storage=sessions, task_storage=tasks,
        engine=engine, source="hook", signal=signal,
    ) == "advanced"
    fresh = await sessions.get("s1")
    assert human_key in fresh.parked_event_keys
    assert fresh.parked_state["resume_event_payloads"][signal.event_key]["payload"] == {"tool_wait_ready": True}


@pytest.mark.asyncio
async def test_malformed_graph_sibling_does_not_block_valid_batch(caplog) -> None:
    sessions, tasks, signal = _world()
    row = await sessions.get("s1")
    sessions._data["s1"] = row.model_copy(update={
        "parked_state": {
            "graph_checkpoint": {"pending_tool_waits": [
                {
                    "node_id": "x",
                    "outstanding_task_ids": list(signal.required_terminal_task_ids),
                    "notifying_results": [],
                },
                {
                    "node_id": "bad",
                    "outstanding_task_ids": ["s1/bad:tool:0:1", 7],
                    "notifying_results": [],
                },
            ]},
        },
    })
    assert await verify_and_mark_batch_resumable(
        session_id="s1", session_storage=sessions, task_storage=tasks,
        engine=_Engine(), source="hook", signal=signal,
    ) == "advanced"
    assert "ignored 1 malformed batch proof" in caplog.text


@pytest.mark.asyncio
async def test_unkeyable_graph_sibling_is_counted_while_valid_batch_wakes(caplog) -> None:
    sessions, tasks, signal = _world()
    row = await sessions.get("s1")
    sessions._data["s1"] = row.model_copy(update={
        "parked_state": {
            "graph_checkpoint": {"pending_tool_waits": [
                {
                    "node_id": "x",
                    "outstanding_task_ids": list(signal.required_terminal_task_ids),
                    "notifying_results": [],
                },
                {
                    "node_id": "bad",
                    "outstanding_task_ids": [7],
                    "notifying_results": [],
                },
            ]},
        },
    })
    assert await verify_and_mark_batch_resumable(
        session_id="s1", session_storage=sessions, task_storage=tasks,
        engine=_Engine(), source="hook", signal=signal,
    ) == "advanced"
    assert "ignored 1 malformed batch proof" in caplog.text


@pytest.mark.asyncio
async def test_all_unkeyable_graph_batches_are_malformed_not_not_ready(caplog) -> None:
    sessions, tasks, signal = _world()
    row = await sessions.get("s1")
    sessions._data["s1"] = row.model_copy(update={
        "parked_state": {
            "graph_checkpoint": {"pending_tool_waits": [
                {
                    "node_id": "bad",
                    "outstanding_task_ids": [7],
                    "notifying_results": [],
                },
                None,
            ]},
        },
    })
    assert await verify_and_mark_batch_resumable(
        session_id="s1", session_storage=sessions, task_storage=tasks,
        engine=_Engine(), source="hook", signal=signal,
    ) == "corrupt"
    assert "ignored 2 malformed batch proof" in caplog.text


@pytest.mark.asyncio
async def test_all_invalid_graph_batches_are_malformed_not_not_ready() -> None:
    sessions, tasks, signal = _world()
    row = await sessions.get("s1")
    duplicate = signal.required_terminal_task_ids[0]
    sessions._data["s1"] = row.model_copy(update={
        "parked_state": {
            "graph_checkpoint": {"pending_tool_waits": [
                {"node_id": "A", "outstanding_task_ids": [duplicate], "notifying_results": []},
                {"node_id": "B", "outstanding_task_ids": [duplicate], "notifying_results": []},
            ]},
        },
    })
    assert await verify_and_mark_batch_resumable(
        session_id="s1", session_storage=sessions, task_storage=tasks,
        engine=_Engine(), source="hook", signal=signal,
    ) == "corrupt"


@pytest.mark.asyncio
async def test_crash_after_flip_is_repaired_without_second_write() -> None:
    sessions, tasks, signal = _world()
    engine = _Engine(fail_once=True)
    with pytest.raises(RuntimeError, match="lost after row flip"):
        await verify_and_mark_batch_resumable(
            session_id="s1", session_storage=sessions, task_storage=tasks,
            engine=engine, source="hook", signal=signal,
        )
    flipped = await sessions.get("s1")
    assert flipped.parked_status == "resumable" and flipped.resumable_at is not None
    stamp = flipped.resumable_at
    assert await verify_and_mark_batch_resumable(
        session_id="s1", session_storage=sessions, task_storage=tasks,
        engine=engine, source="periodic", now=stamp + timedelta(seconds=31),
    ) == "lease_rearmed"
    assert (await sessions.get("s1")).resumable_at == stamp


@pytest.mark.parametrize(
    "update",
    [
        {"binding_epoch": 2},
        {"turn_no": 1},
        {
            "parked_at": _now() + timedelta(seconds=1),
            "parked_event_key": "tool_wait:s1:x:tool:9:1",
            "parked_state": {
                "kind": "tool_wait",
                "outstanding_task_ids": ["s1/x:tool:9:1"],
                "notifying_task_ids": [],
            },
        },
        {"pause_requested": True, "pause_requested_at": _now()},
        {"cancel_requested": True, "cancel_requested_at": _now()},
    ],
    ids=["binding", "turn", "park_event", "pause", "cancel"],
)
@pytest.mark.asyncio
async def test_fresh_occurrence_cas_refuses_concurrent_replacement(update) -> None:
    old_sessions, tasks, signal = _world()
    sessions = _RaceStorage(update)
    sessions._data["s1"] = await old_sessions.get("s1")
    engine = _Engine()
    assert await verify_and_mark_batch_resumable(
        session_id="s1", session_storage=sessions, task_storage=tasks,
        engine=engine, source="hook", signal=signal,
    ) == "stale"
    fresh = await sessions.get("s1")
    assert fresh.parked_status == "parked"
    assert engine.marks == 0


@pytest.mark.asyncio
async def test_session_adapter_owns_marker_for_tool_and_classic_parks() -> None:
    sessions, _, signal = _world()
    row = await sessions.get("s1")
    sessions._data["s1"] = row.model_copy(update={
        "parked_status": None, "parked_event_key": None, "parked_at": None,
        "parked_state": None, "parked_tool_batches": None,
    })
    adapter = SessionClaimAdapter(session_storage=sessions)
    await adapter.on_release(conn=None, entity_id="s1", outcome=ReleaseOutcome(
        success=True,
        park=ParkRequest(
            parked_state={"kind": "tool_wait", "outstanding_task_ids": list(signal.required_terminal_task_ids), "notifying_task_ids": []},
            parked_event_key=signal.event_key, parked_until=None, parked_at=_now(),
        ),
    ))
    assert (await sessions.get("s1")).parked_tool_batches is True
    await adapter.on_release(
        conn=None, entity_id="s1", outcome=ReleaseOutcome(success=True),
    )
    cleared = await sessions.get("s1")
    assert cleared.parked_tool_batches is None and cleared.resumable_at is None


@pytest.mark.asyncio
async def test_periodic_scan_is_bounded_and_carries_its_cursor() -> None:
    provider = _FakeStorageProvider()
    sessions = provider.get_storage(WorkspaceSession)
    template = (await _world()[0].get("s1"))
    assert template is not None
    for index in range(200):
        session_id = f"a-{index:03d}"
        sessions._data[session_id] = template.model_copy(update={
            "id": session_id,
            "parked_state": {
                "kind": "tool_wait",
                "outstanding_task_ids": [7],
                "notifying_task_ids": [],
            },
        })
    ready_sessions, ready_tasks, _ = _world()
    sessions._data["s1"] = await ready_sessions.get("s1")
    provider.get_storage(ToolCallTask)._data.update(ready_tasks._data)

    cursor, count = await reconcile_terminal_tool_wait_parks(
        provider, _Engine(), source="periodic",
    )
    assert count == 200 and cursor is not None
    next_cursor, next_count = await reconcile_terminal_tool_wait_parks(
        provider, _Engine(), source="periodic", cursor=cursor,
    )
    assert next_count == 1 and next_cursor is None
    assert (await sessions.get("s1")).parked_status == "resumable"
    wrapped_cursor, wrapped_count = await reconcile_terminal_tool_wait_parks(
        provider, _Engine(), source="periodic", cursor=next_cursor,
    )
    assert wrapped_count == 200 and wrapped_cursor is not None


@pytest.mark.asyncio
async def test_reconciler_carries_cursor_across_leadership_loss_and_stops(
    monkeypatch,
) -> None:
    calls: list[str | None] = []
    first_tick = asyncio.Event()
    second_tick = asyncio.Event()

    async def _fake_reconcile(_provider, _engine, *, source, cursor=None):
        assert source == "periodic"
        calls.append(cursor)
        (first_tick if len(calls) == 1 else second_tick).set()
        return ("next" if len(calls) == 1 else None), 1

    monkeypatch.setattr(
        reconcile_module, "reconcile_terminal_tool_wait_parks", _fake_reconcile,
    )

    class _Lease:
        def __init__(self) -> None:
            self.lost_event = asyncio.Event()
            self.released = False

        async def release(self) -> None:
            self.released = True

    class _Elector:
        def __init__(self, leases) -> None:
            self.leases = list(leases)

        async def try_acquire(self, role):
            assert role == "tool-wait-reconciler"
            return self.leases.pop(0)

    first, second = _Lease(), _Lease()
    reconciler = ToolWaitWakeReconciler(
        storage_provider=object(), claim_engine=_Engine(), poll_seconds=3600,
    )
    reconciler.start(_Elector([first, second]))
    await asyncio.wait_for(first_tick.wait(), timeout=1)
    first.lost_event.set()
    await asyncio.wait_for(second_tick.wait(), timeout=1)
    await reconciler.stop()
    assert calls == [None, "next"]
    assert first.released and second.released


@pytest.mark.asyncio
async def test_outcome_metrics_use_closed_vocabulary_once_per_source(monkeypatch) -> None:
    labels: list[tuple[str, str]] = []

    class _Counter:
        def labels(self, source, outcome):
            labels.append((source, outcome))
            return self

        def inc(self, amount=1):
            assert amount == 1

    monkeypatch.setattr(
        reconcile_module._metrics, "tool_wait_reconcile_outcomes_total", _Counter(),
    )
    sessions, tasks, signal = _world()
    await verify_and_mark_batch_resumable(
        session_id="s1", session_storage=sessions, task_storage=tasks,
        engine=_Engine(), source="hook", signal=signal,
    )
    provider = _FakeStorageProvider()
    nonterminal_sessions, nonterminal_tasks, _ = _world(
        task_state=ToolCallTaskState.QUEUED,
    )
    provider.get_storage(WorkspaceSession)._data.update(nonterminal_sessions._data)
    provider.get_storage(ToolCallTask)._data.update(nonterminal_tasks._data)
    await reconcile_terminal_tool_wait_parks(provider, _Engine(), source="boot")
    await verify_and_mark_batch_resumable(
        session_id="missing", session_storage=nonterminal_sessions,
        task_storage=nonterminal_tasks, engine=_Engine(), source="periodic",
    )
    assert labels == [
        ("hook", "woken"),
        ("boot", "not_ready"),
        ("periodic", "missing"),
    ]
