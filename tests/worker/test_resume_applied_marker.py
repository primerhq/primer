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
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.observability import metrics
from primer.worker.session_resume_coordinator import inject_resume_and_continue

from tests.conftest import _FakeStorageProvider
from tests.worker.test_engine_session_resume import (
    _build_engine,
    _build_pool,
    _make_resumable_session,
    _FakeWorkspaceIO,
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


def _warnings(caplog, needle: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and needle in r.getMessage()]


class _World:
    """The production shape: the workspace ACCEPTS the TOOL_RESULT record write (``_FakeWorkspaceIO``), so the helper that persists it runs to the end. The
    first version of these tests used ``_NoopPersist``, whose record write fails, and so never exercised what that write does to the row (#681 review, B1)."""

    def __init__(self, monkeypatch, *, abandon_releases: int = 0, executor=None, workspace=None) -> None:
        self.storage = _FakeStorageProvider()
        self.sessions = self.storage.get_storage(WorkspaceSession)
        self.engine = _build_engine(self.sessions)
        self.pool = _build_pool(self.storage, self.engine)
        if abandon_releases:        # a bound this short is only needed for an abandoned release, and it risks a flake on a slow host otherwise
            self.pool._release_timeout_seconds = 0.3
            self.pool._release_probe_timeout_seconds = 0.3
        self.executor = executor or _RecordingExecutor()
        self.workspace = workspace if workspace is not None else _FakeWorkspaceIO()
        monkeypatch.setattr(self.pool, "_load_workspace_for_persist", lambda _w: _async_return(self.workspace))
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
    assert row.last_seq == 1, "the TOOL_RESULT record was written and last_seq advanced past it"


@pytest.mark.asyncio
async def test_a_failing_record_write_still_lets_the_marker_land(monkeypatch):
    """The record write is best-effort (``_NoopPersist`` has no ``append_message_line``, so it fails and is swallowed): the marker does not depend on it."""
    world = _World(monkeypatch, workspace=_NoopPersist())
    seeded = await world.seed()
    await world.engine.mark_resumable(ClaimKind.SESSION, SID)

    await world.claim_and_run("wrk-engine-resume")

    row = await world.sessions.get(SID)
    assert row.resumed_park_at == seeded.parked_at, "the marker is written whether or not the record write succeeded"
    assert row.last_seq == 0, "the failed record write advanced nothing"
    assert len(world.executor.injected) == 1


@pytest.mark.asyncio
async def test_the_marker_and_the_advanced_last_seq_both_survive_an_abandoned_release(monkeypatch):
    """B1: with a workspace that accepts the record, the abandoned release leaves BOTH writes on the row (the order of the two must not matter)."""
    world = _World(monkeypatch, abandon_releases=1)
    seeded = await world.seed()
    await world.engine.mark_resumable(ClaimKind.SESSION, SID)

    await world.claim_and_run("wrk-engine-resume")

    row = await world.sessions.get(SID)
    assert row.parked_status == "resumable" and row.last_seq == 1 and row.resumed_park_at == seeded.parked_at


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
    lines = _warnings(caplog, "already applied")
    assert len(lines) == 1 and SID in lines[0], "one WARNING for the one skip"


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


async def _mark(world, snapshot) -> None:
    from primer.worker.session_resume_coordinator import _mark_resume_applied

    await _mark_resume_applied(world.pool, snapshot)


@pytest.mark.asyncio
async def test_the_marker_is_written_for_the_park_the_row_still_carries(monkeypatch):
    world = _World(monkeypatch)
    seeded = await world.seed()

    await _mark(world, seeded)

    assert (await world.sessions.get(SID)).resumed_park_at == seeded.parked_at


@pytest.mark.asyncio
async def test_the_marker_is_not_written_for_a_park_the_row_no_longer_carries(monkeypatch):
    """B2: the fence is what the marker NAMES (the park, by ``parked_at``), not the turn: a row that parked again since (a new ``parked_at``) is not given
    a marker for the old park, whatever its ``turn_no``."""
    world = _World(monkeypatch)
    seeded = await world.seed()
    reparked = seeded.model_copy(update={"parked_at": seeded.parked_at + timedelta(seconds=30)})
    await world.sessions.update(reparked)

    await _mark(world, seeded)          # the snapshot is the pool-start row; the stored row is a later park

    assert (await world.sessions.get(SID)).resumed_park_at is None


@pytest.mark.asyncio
async def test_the_marker_is_not_written_once_the_row_is_no_longer_resumable(monkeypatch):
    """The release committed meanwhile (the park columns are cleared): there is nothing left to mark, and a marker would outlive its park."""
    world = _World(monkeypatch)
    seeded = await world.seed()
    await world.sessions.update(seeded.model_copy(update={"parked_status": None, "parked_at": None, "turn_no": 1}))

    await _mark(world, seeded)

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


# ---- the root fix: the TOOL_RESULT record write touches last_seq and nothing else ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_resume_does_not_put_back_a_stop_the_pool_just_cleared(monkeypatch):
    """P8 (main too): the pool clears a Stop recorded before the park it resolves, then the record write used to put the pool-start copy of the whole row back,
    ``interrupt_requested`` included, so the continuation was killed at its first poll."""
    world = _World(monkeypatch)
    await world.seed(interrupt_requested=True)
    await world.engine.mark_resumable(ClaimKind.SESSION, SID)

    await world.claim_and_run("wrk-engine-resume")

    assert (await world.sessions.get(SID)).interrupt_requested is False


@pytest.mark.asyncio
async def test_the_record_write_leaves_what_a_concurrent_writer_changed_alone(monkeypatch):
    """A steer that lands while the handler runs sets ``turn_status`` to claimable; the record write used to put the pool-start value back."""

    class _SteeredDuringTheHandler(_RecordingExecutor):
        async def inject_resume_messages(self, messages):
            await super().inject_resume_messages(messages)
            await world.sessions.patch_if(SID, {"turn_status": "claimable"}, where={"workspace_id": [f"ws-{SID}"]})

    world = _World(monkeypatch, executor=_SteeredDuringTheHandler())
    await world.seed()
    await world.engine.mark_resumable(ClaimKind.SESSION, SID)

    await world.claim_and_run("wrk-engine-resume")

    assert (await world.sessions.get(SID)).turn_status == "claimable"


# ---- the skip's OUTCOME -------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_skip_clears_the_park_bumps_the_turn_and_the_continuation_runs_on_the_next_claim(monkeypatch):
    calls: list[str] = []

    async def fake_turn(lease, deps):
        from primer.int.claim import ReleaseOutcome

        calls.append(lease.entity_id)
        return ReleaseOutcome(success=True, drop_lease=True)

    monkeypatch.setattr("primer.worker.pool.run_one_session_turn", fake_turn)
    world = _World(monkeypatch, abandon_releases=1)
    seeded = await world.seed()
    await world.engine.mark_resumable(ClaimKind.SESSION, SID)
    await world.claim_and_run("wrk-engine-resume")
    await world.expire_lease()
    await world.claim_and_run("wrk-engine-resume-2")          # the skip

    skipped = await world.sessions.get(SID)
    assert (skipped.parked_status, skipped.parked_at, skipped.turn_no) == (None, None, 1)
    assert skipped.resumed_park_at == seeded.parked_at and calls == [], "the skip itself ran no turn"

    await world.claim_and_run("wrk-engine-resume-3")          # the continuation
    assert calls == [SID], "the next claim runs the continuation turn"
    assert len(world.executor.injected) == 1


@pytest.mark.asyncio
async def test_a_cancel_requested_after_the_first_attempt_ends_the_session_instead_of_skipping(monkeypatch):
    world = _World(monkeypatch, abandon_releases=1)
    await world.seed()
    await world.engine.mark_resumable(ClaimKind.SESSION, SID)
    await world.claim_and_run("wrk-engine-resume")
    await world.sessions.patch_if(SID, {"cancel_requested": True}, where={"workspace_id": [f"ws-{SID}"]})
    await world.expire_lease()

    await world.claim_and_run("wrk-engine-resume-2")

    row = await world.sessions.get(SID)
    assert row.status == SessionStatus.ENDED and row.ended_reason == "cancelled"
    assert _noops() == 0 and len(world.executor.injected) == 1


@pytest.mark.asyncio
async def test_a_pause_keeps_the_park_and_the_resume_after_it_skips_the_handler(monkeypatch):
    world = _World(monkeypatch, abandon_releases=1)
    seeded = await world.seed()
    await world.engine.mark_resumable(ClaimKind.SESSION, SID)
    await world.claim_and_run("wrk-engine-resume")
    await world.sessions.patch_if(SID, {"pause_requested": True}, where={"workspace_id": [f"ws-{SID}"]})
    await world.expire_lease()

    await world.claim_and_run("wrk-engine-resume-2")          # the pause exit: PAUSED, the park is kept for /resume

    paused = await world.sessions.get(SID)
    assert paused.status == SessionStatus.PAUSED and paused.parked_status == "resumable"
    assert paused.resumed_park_at == seeded.parked_at, (
        "the pool's pause exit re-reads the row and writes it back whole, so the marker the first attempt wrote is still on it "
        "(a whole-document writer that read the row BEFORE the marker was written would drop it: see the docs)"
    )
    await world.sessions.patch_if(SID, {"status": "running", "pause_requested": False}, where={"status": ["paused"]})
    await world.engine.mark_resumable(ClaimKind.SESSION, SID)          # what /resume does

    await world.claim_and_run("wrk-engine-resume-3")

    assert len(world.executor.injected) == 1 and _noops() == 1, "the handler is not run again after /resume"
    assert (await world.sessions.get(SID)).parked_status is None


# ---- the guard ----------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker,parked,expected",
    [(None, None, False), (None, "t", False), ("t", None, False), ("t", "t", True), ("t1", "t2", False)],
    ids=["neither", "no-marker", "no-park", "equal", "older-park"],
)
def test_resume_already_applied_is_true_only_for_a_marker_that_names_the_park(marker, parked, expected):
    from primer.worker.session_resume_coordinator import resume_already_applied

    t = datetime(2026, 10, 9, tzinfo=timezone.utc)
    value = {None: None, "t": t, "t1": t, "t2": t + timedelta(seconds=1)}
    row = SimpleNamespace(resumed_park_at=value[marker], parked_at=value[parked])

    assert resume_already_applied(row) is expected
