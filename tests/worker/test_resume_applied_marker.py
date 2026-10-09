"""A resume whose release never committed is not applied twice (ticket 01a10b54-425b).

A resume handler does its effects (injects the operator's reply, appends the TOOL_RESULT record, runs an approved tool) and
only the RELEASE that follows clears the park. A release that is abandoned at the pool's bound, or raises, rolls back, so the
row still says ``parked_status="resumable"`` and the re-claim took the resume branch again: the reply was injected twice and an
approved tool ran twice. ``WorkspaceSession.resumed_park_at`` is the marker (the same shape as ``completed_turn_no``): the
continue path writes the ``parked_at`` of the park it applied, after its effects and before the release, and the resume branch
of the pool treats a row whose marker equals its ``parked_at`` as already applied: it does not run the handler, it returns the
outcome the handler would have returned (the release then clears the park and bumps ``turn_no``, once).

These tests drive the real pool, claim engine and session adapter; the only stand-ins are the executor and the workspace.
``tests/worker/test_release_rollback_reproductions.py`` holds the scenario this fixes (its behaviour test is the plain
"the handler does not run again" assertion; these tests pin the rest of the contract).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from primer.int.claim import ClaimKind
from primer.model.chat import Message, ToolCallPart, ToolResultPart
from primer.model.workspace_session import WorkspaceSession
from primer.observability import metrics
from primer.worker.session_resume_coordinator import inject_resume_and_continue

from tests.conftest import _FakeStorageProvider
from tests.worker.test_engine_session_resume import (
    _build_engine,
    _build_pool,
    _make_resumable_session,
    _NoopPersist,
    _RecordingExecutor,
    _async_return,
)

SID, TCID = "sess-resume-marker", "tc-ask-1"


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


def _noops() -> float:
    return metrics.session_resume_noop_total._value.get()


class _World:
    def __init__(self, monkeypatch, *, abandon_releases: int = 0, executor=None) -> None:
        self.storage = _FakeStorageProvider()
        self.sessions = self.storage.get_storage(WorkspaceSession)
        self.engine = _build_engine(self.sessions)
        self.pool = _build_pool(self.storage, self.engine)
        self.pool._release_timeout_seconds = 0.3
        self.pool._release_probe_timeout_seconds = 0.3
        self.executor = executor or _RecordingExecutor()
        monkeypatch.setattr(self.pool, "_load_workspace_for_persist", lambda _w: _async_return(_NoopPersist()))
        monkeypatch.setattr(self.pool, "_build_agent_executor", lambda _s, _w: _async_return(self.executor))
        self.abandoned = 0
        real_release = self.engine.release
        world = self

        async def release(lease, *, outcome):
            if world.abandoned < abandon_releases:
                world.abandoned += 1
                await asyncio.Event().wait()  # never answers: the real release (and its on_release) never runs
            return await real_release(lease, outcome=outcome)

        self.engine.release = release  # type: ignore[method-assign]

    async def seed(self, **fields) -> WorkspaceSession:
        assistant = Message(
            role="assistant", parts=[ToolCallPart(id=TCID, name="_misc__ask_user", arguments={"prompt": "name?"})]
        )
        row = _make_resumable_session(
            SID, tool_name="ask_user", tool_call_id=TCID, resume_event_payload={"response": "Alice"},
            llm_messages=[assistant.model_dump(mode="json")],
        )
        row = row.model_copy(update=fields)
        await self.sessions.create(row)
        return row

    async def claim_and_run(self, worker: str) -> None:
        [lease] = await self.engine.claim_due(worker, max_count=10)
        await self.pool._run_engine_session(lease)

    async def expire_lease(self) -> None:
        self.engine._leases[(ClaimKind.SESSION, SID)].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)


@pytest.mark.asyncio
async def test_a_resume_that_continues_records_the_park_it_applied(monkeypatch):
    world = _World(monkeypatch)
    seeded = await world.seed()
    await world.engine.mark_resumable(ClaimKind.SESSION, SID)

    await world.claim_and_run("wrk-engine-resume")

    row = await world.sessions.get(SID)
    assert row.parked_status is None and row.turn_no == 1, "the resume did not release normally"
    assert row.resumed_park_at == seeded.parked_at, "the continue path must record which park it applied"
    assert len(world.executor.injected) == 1


@pytest.mark.asyncio
async def test_the_skip_is_counted_and_logged_once(monkeypatch, caplog):
    world = _World(monkeypatch, abandon_releases=1)
    await world.seed()
    await world.engine.mark_resumable(ClaimKind.SESSION, SID)
    await world.claim_and_run("wrk-engine-resume")
    assert _noops() == 0, "the first resume ran the handler: nothing to count"
    await world.expire_lease()

    with caplog.at_level(logging.WARNING, logger="primer.worker.pool"):
        await world.claim_and_run("wrk-engine-resume-2")

    assert _noops() == 1
    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert SID in text and "already applied" in text, text


@pytest.mark.asyncio
async def test_the_skip_leaves_the_continuation_armed_like_a_resume_that_ran(monkeypatch):
    """The outcome of a skipped resume is the handler's own (success, lease row kept and unclaimed), so the next claim runs the continuation turn."""
    world = _World(monkeypatch, abandon_releases=1)
    await world.seed()
    await world.engine.mark_resumable(ClaimKind.SESSION, SID)
    await world.claim_and_run("wrk-engine-resume")
    await world.expire_lease()
    await world.claim_and_run("wrk-engine-resume-2")

    held = world.engine._leases.get((ClaimKind.SESSION, SID))
    assert held is not None, "a continue outcome keeps the lease row (a drop would delete it)"
    assert held.claimed_by is None, "and hands the claim back, so the next claim can run the continuation"
    assert [lease.entity_id for lease in await world.engine.claim_due("wrk-engine-resume-3", max_count=10)] == [SID]


@pytest.mark.asyncio
async def test_a_later_park_with_a_different_parked_at_runs_its_handler(monkeypatch):
    """The marker names ONE park. A row whose marker is an older park's is not skipped."""
    world = _World(monkeypatch)
    older = datetime.now(timezone.utc) - timedelta(hours=1)
    await world.seed(resumed_park_at=older)
    await world.engine.mark_resumable(ClaimKind.SESSION, SID)

    await world.claim_and_run("wrk-engine-resume")

    assert len(world.executor.injected) == 1
    assert _noops() == 0


@pytest.mark.asyncio
async def test_a_row_without_the_marker_runs_its_handler(monkeypatch):
    """Rows written before the field existed read ``None`` and behave as they did."""
    world = _World(monkeypatch)
    await world.seed()
    assert (await world.sessions.get(SID)).resumed_park_at is None
    await world.engine.mark_resumable(ClaimKind.SESSION, SID)

    await world.claim_and_run("wrk-engine-resume")

    assert len(world.executor.injected) == 1


class _FailingInject(_RecordingExecutor):
    async def inject_resume_messages(self, messages):
        raise RuntimeError("workspace write failed")


@pytest.mark.asyncio
async def test_a_resume_that_fails_does_not_record_the_park(monkeypatch):
    """Only the continue path writes the marker: a persist failure ends the session and nothing was applied."""
    world = _World(monkeypatch, executor=_FailingInject())
    await world.seed()
    await world.engine.mark_resumable(ClaimKind.SESSION, SID)

    await world.claim_and_run("wrk-engine-resume")

    row = await world.sessions.get(SID)
    assert row.resumed_park_at is None


def _parked() -> SimpleNamespace:
    assistant = Message(role="assistant", parts=[ToolCallPart(id=TCID, name="_misc__ask_user", arguments={})])
    return SimpleNamespace(
        llm_messages=[assistant.model_dump(mode="json")], tool_call_id=TCID, scoped_tool_call_id=None,
    )


@pytest.mark.asyncio
async def test_the_marker_is_fenced_on_the_turn_it_was_written_for(monkeypatch):
    """A row that has moved to another turn (the release committed meanwhile) is not given a marker for the old park."""
    world = _World(monkeypatch)
    seeded = await world.seed()
    moved = seeded.model_copy(update={"turn_no": 0})
    await world.sessions.update(seeded.model_copy(update={"turn_no": 3}))

    await inject_resume_and_continue(
        world.pool, moved, world.executor, _parked(), ToolResultPart(id=TCID, output="{}", error=False),
    )

    assert (await world.sessions.get(SID)).resumed_park_at is None


@pytest.mark.asyncio
async def test_a_marker_that_cannot_be_written_does_not_fail_the_resume(monkeypatch, caplog):
    """Best-effort, like ``completed_turn_no``: without the marker a rolled-back release runs the handler again, as it did."""
    world = _World(monkeypatch)
    await world.seed()
    real_patch_if = world.sessions.patch_if

    async def patch_if(id, patch=None, *, where, set_paths=None, conn=None):
        if patch and "resumed_park_at" in patch:
            raise RuntimeError("storage down")
        return await real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)

    world.sessions.patch_if = patch_if  # type: ignore[method-assign]
    await world.engine.mark_resumable(ClaimKind.SESSION, SID)

    with caplog.at_level(logging.WARNING):
        await world.claim_and_run("wrk-engine-resume")

    row = await world.sessions.get(SID)
    assert row.parked_status is None and row.turn_no == 1, "the resume must still complete"
    assert any("resumed" in r.getMessage() and SID in r.getMessage() for r in caplog.records)
