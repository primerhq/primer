"""A park whose release never committed is applied by the next claim, not re-run (ticket 01a10b54-3212).

A turn that parks has already committed its records (the YIELDED record, a tool_wait batch's ToolCallTask rows, ``last_seq``) when it
returns its park outcome; the park COLUMNS exist only inside the release transaction (``SessionClaimAdapter.on_release``). A release
abandoned at the pool's bound, or raising, rolls back, and the re-claim used to run a fresh turn over a log that ends in a dangling
``yielded`` record: a second model call at the same ``turn_no`` (and, with tool calls as claims on, different record seqs that end
the session ``failed``).

``WorkspaceSession.pending_park`` is the stash: the park arms write ``{"turn_no": n, "park": <the ParkRequest>}`` as one field-scoped
``patch_if`` fenced on ``turn_no`` after their last durable write and before the release, the release's park branch clears it in the
same transaction, and a claim that finds a stash for its own ``turn_no`` returns that park as its outcome instead of running the turn
(``_apply_pending_park_if_stashed`` in ``primer/session/dispatch.py``). The scenarios are in ``test_release_rollback_reproductions.py``;
these tests pin the rest of the contract through the real pool, claim engine and session adapter.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest
from pydantic_core import to_jsonable_python

import primer.observability.metrics as metrics
import primer.session.dispatch as session_dispatch
from primer.int.claim import ClaimKind, ParkRequest, ReleaseOutcome
from primer.model.workspace_session import SessionMessageKind, SessionStatus
from primer.workspace.session_factory import pause_session

from tests.worker.test_completed_turn_reclaim import SID, WS, _World, world  # noqa: F401
from tests.worker.test_release_rollback_reproductions import (
    _AskThenAnswer,
    _park_release_abandoned_then_reclaimed,
    _tool_wait_park_release_abandoned_then_reclaimed,
)


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


def _applied() -> float:
    return metrics.session_park_stash_applied_total._value.get()


async def _leave_a_lost_park(world) -> None:
    """Run the first half of the rollback reproduction: the park release never reaches its adapter."""
    await world.create_session()
    await world.steer("first")
    pool = world.pool("wrk-a")
    pool._build_session_executor = lambda s: _async(_AskThenAnswer(world, s))  # type: ignore[method-assign]
    world.abandon_next_releases = 1
    assert await world.claim_and_run(pool) == 1
    row = await world.row()
    assert row.pending_park is not None and row.parked_status is None


@pytest.mark.asyncio
async def test_the_park_turn_stashes_the_park_it_is_about_to_release(world):
    seen = await _park_release_abandoned_then_reclaimed(world)

    stash = seen["row_after_park_turn"].pending_park
    assert stash is not None, "the park arm must stash its park before the release"
    assert stash["turn_no"] == 0
    assert stash["park"]["parked_event_key"] == f"ask_user:{SID}:tc-1"


@pytest.mark.asyncio
async def test_the_re_claim_returns_the_park_the_first_attempt_made(world):
    await _park_release_abandoned_then_reclaimed(world)

    first, second = world.outcomes
    assert first.park is not None
    assert second.park == first.park, "the re-claim must hand on the stashed park unchanged"
    assert (second.success, second.drop_lease) == (True, True)


@pytest.mark.asyncio
async def test_the_applied_park_release_writes_the_park_and_clears_the_stash(world):
    seen = await _park_release_abandoned_then_reclaimed(world)

    row = seen["row_after_reclaim"]
    assert row.parked_status == "parked" and row.parked_event_key == f"ask_user:{SID}:tc-1"
    assert row.pending_park is None, "the release that applies a park clears its stash in the same transaction"
    assert row.turn_no == 0, "a park is not a completed turn"
    assert seen["model_calls"] == 1


@pytest.mark.asyncio
async def test_the_re_claim_is_counted_and_logged_once(world, caplog):
    with caplog.at_level(logging.WARNING, logger="primer.session.dispatch"):
        await _park_release_abandoned_then_reclaimed(world)

    assert _applied() == 1
    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert SID in text and "already parked" in text, text


@pytest.mark.asyncio
async def test_a_pause_after_the_dispatch_snapshot_wins_over_the_stashed_park(world, monkeypatch):
    """A pause in the old top-read/fresh-stash-read window must not create an unclaimable RUNNING parked row."""
    await _leave_a_lost_park(world)
    world.expire_lease()
    real_noop = session_dispatch._noop_if_turn_already_completed
    raced = False

    async def pause_between_guards(*args):
        nonlocal raced
        result = await real_noop(*args)
        if not raced:
            raced = True
            written = await world.sessions.patch_if(
                SID,
                {"pause_requested": True},
                where={"status": [SessionStatus.RUNNING.value], "pause_requested": [False]},
            )
            assert written is not None
        return result

    monkeypatch.setattr(session_dispatch, "_noop_if_turn_already_completed", pause_between_guards)
    pool = world.pool("wrk-b")
    pool._build_session_executor = lambda s: _async(_AskThenAnswer(world, s))  # type: ignore[method-assign]

    assert await world.claim_and_run(pool) == 1

    outcome = world.outcomes[-1]
    row = await world.row()
    assert outcome.park is None and outcome.preserve_park is True
    assert row.status is SessionStatus.PAUSED
    assert row.parked_status is None
    assert row.pause_requested is True
    assert row.pending_park is None
    assert len(world.llm_calls) == 1, "the paused re-claim must not build or run a second turn"


@pytest.mark.asyncio
async def test_a_cancel_after_the_dispatch_snapshot_wins_over_the_stashed_park(world, monkeypatch):
    await _leave_a_lost_park(world)
    world.expire_lease()
    real_noop = session_dispatch._noop_if_turn_already_completed

    async def cancel_between_guards(*args):
        result = await real_noop(*args)
        written = await world.sessions.patch_if(
            SID,
            {"cancel_requested": True},
            where={"status": [SessionStatus.RUNNING.value], "cancel_requested": [False]},
        )
        assert written is not None
        return result

    monkeypatch.setattr(session_dispatch, "_noop_if_turn_already_completed", cancel_between_guards)
    assert await world.claim_and_run(world.pool("wrk-b")) == 1

    outcome = world.outcomes[-1]
    row = await world.row()
    assert outcome.park is None
    assert row.status is SessionStatus.ENDED and row.ended_reason == "cancelled"
    assert row.pending_park is None
    assert len(world.llm_calls) == 1


@pytest.mark.asyncio
async def test_a_pause_after_the_stash_guard_aborts_the_park_release_and_wins_on_reclaim(world):
    """The release transaction fences the last gap: after guard return, before adapter write."""
    await _leave_a_lost_park(world)
    world.expire_lease()
    real_release = world.engine.release
    paused = False

    async def pause_before_release(lease, *, outcome):
        nonlocal paused
        if outcome.park is not None and not paused:
            paused = True
            await pause_session(
                workspace_id=WS,
                session_id=SID,
                sessions=world.sessions,
            )
        return await real_release(lease, outcome=outcome)

    world.engine.release = pause_before_release  # type: ignore[method-assign]
    pool = world.pool("wrk-late-pause")
    pool._build_session_executor = lambda s: _async(_AskThenAnswer(world, s))  # type: ignore[method-assign]
    assert await world.claim_and_run(pool) == 1

    deferred = await world.row()
    assert deferred.status is SessionStatus.RUNNING and deferred.pause_requested is True
    assert deferred.parked_status is None, "the refused release must not create an ineligible parked orphan"
    assert deferred.pending_park is not None
    assert len(world.llm_calls) == 1

    world.expire_lease()
    assert await world.claim_and_run(world.pool("wrk-pause-reclaim")) == 1
    paused_row = await world.row()
    assert paused_row.status is SessionStatus.PAUSED
    assert paused_row.parked_status is None and paused_row.pending_park is None
    assert len(world.llm_calls) == 1, "the pause reclaim must not call the model again"


@pytest.mark.parametrize("live_status", [SessionStatus.WAITING, SessionStatus.CREATED])
@pytest.mark.asyncio
async def test_a_live_status_change_after_the_stash_guard_still_applies_the_park(world, live_status):
    await _leave_a_lost_park(world)
    world.expire_lease()
    real_release = world.engine.release
    moved = False

    async def move_before_release(lease, *, outcome):
        nonlocal moved
        if outcome.park is not None and not moved:
            moved = True
            written = await world.sessions.patch_if(
                SID,
                {"status": live_status.value},
                where={"status": [SessionStatus.RUNNING.value]},
            )
            assert written is not None
        return await real_release(lease, outcome=outcome)

    world.engine.release = move_before_release  # type: ignore[method-assign]
    pool = world.pool(f"wrk-late-{live_status.value}")
    pool._build_session_executor = lambda s: _async(_AskThenAnswer(world, s))  # type: ignore[method-assign]

    assert await world.claim_and_run(pool) == 1

    row = await world.row()
    assert moved
    assert row.status is live_status and row.parked_status == "parked"
    assert row.pending_park is None
    assert len(world.llm_calls) == 1, "the recovered park must not run the model again"
    assert not await world.engine.has_lease(ClaimKind.SESSION, SID)


@pytest.mark.asyncio
async def test_a_graph_repark_after_a_live_status_change_drops_the_old_resume_lease(world):
    row = await world.create_session()
    parked_at = datetime.now(timezone.utc)
    await world.sessions.update(row.model_copy(update={
        "status": SessionStatus.RUNNING,
        "parked_status": "resumable",
        "parked_event_key": "graph:old",
        "parked_event_keys": ["graph:old"],
        "parked_at": parked_at,
        "parked_state": {"resume_event_payloads": {}, "resume_event_key": "graph:old"},
    }))
    await world.engine.upsert(ClaimKind.SESSION, SID)
    leases = await world.engine.claim_due("wrk-graph-resume", max_count=1)
    assert len(leases) == 1

    outcome = ReleaseOutcome(
        success=True,
        drop_lease=True,
        park=ParkRequest(
            parked_state={"kind": "graph", "resume_event_payloads": {}},
            parked_event_key="graph:new",
            parked_event_keys=["graph:new"],
            parked_at=parked_at,
            parked_until=None,
            drained_reply_keys=frozenset(),
        ),
    )
    moved = await world.sessions.patch_if(
        SID,
        {"status": SessionStatus.WAITING.value},
        where={"status": [SessionStatus.RUNNING.value]},
    )
    assert moved is not None

    await world.engine.release(leases[0], outcome=outcome)

    reparking = await world.row()
    assert reparking.status is SessionStatus.WAITING
    assert reparking.parked_status == "parked" and reparking.parked_event_key == "graph:new"
    assert not await world.engine.has_lease(ClaimKind.SESSION, SID), "the old resume handler must not be re-armed"
    assert await world.engine.claim_due("wrk-graph-again", max_count=1) == []


@pytest.mark.asyncio
async def test_two_refused_pause_writes_finish_with_a_fresh_cancel_decision(world, monkeypatch):
    await _leave_a_lost_park(world)
    world.expire_lease()
    real_noop = session_dispatch._noop_if_turn_already_completed
    real_patch_if = world.sessions.patch_if
    pause_writes = 0

    async def pause_between_guards(*args):
        result = await real_noop(*args)
        written = await real_patch_if(
            SID,
            {"pause_requested": True},
            where={"status": [SessionStatus.RUNNING.value], "pause_requested": [False]},
        )
        assert written is not None
        return result

    async def refuse_both_pause_writes(id, patch=None, *, where, set_paths=None, conn=None):
        nonlocal pause_writes
        if patch and patch.get("status") == SessionStatus.PAUSED.value:
            pause_writes += 1
            if pause_writes == 2:
                cancelled = await real_patch_if(
                    SID,
                    {"cancel_requested": True},
                    where={"pause_requested": [True], "cancel_requested": [False]},
                )
                assert cancelled is not None
            return None
        return await real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)

    monkeypatch.setattr(session_dispatch, "_noop_if_turn_already_completed", pause_between_guards)
    world.sessions.patch_if = refuse_both_pause_writes  # type: ignore[method-assign]
    pool = world.pool("wrk-cas-race")
    pool._build_session_executor = lambda s: _async(_AskThenAnswer(world, s))  # type: ignore[method-assign]
    assert await world.claim_and_run(pool) == 1

    row = await world.row()
    assert pause_writes == 2
    assert row.status is SessionStatus.ENDED and row.ended_reason == "cancelled"
    assert row.parked_status is None and row.pending_park is None
    assert world.outcomes[-1].park is None
    assert len(world.llm_calls) == 1, "CAS exhaustion must not fall through to an executor or model call"


@pytest.mark.asyncio
async def test_two_refused_pause_writes_requeue_entity_noop_when_the_third_read_still_sees_pause(world, monkeypatch):
    await _leave_a_lost_park(world)
    world.expire_lease()
    real_noop = session_dispatch._noop_if_turn_already_completed
    real_patch_if = world.sessions.patch_if
    pause_writes = 0
    adapter_writes = 0

    async def pause_between_guards(*args):
        result = await real_noop(*args)
        written = await real_patch_if(
            SID,
            {"pause_requested": True},
            where={"status": [SessionStatus.RUNNING.value], "pause_requested": [False]},
        )
        assert written is not None
        return result

    async def refuse_both_pause_writes(id, patch=None, *, where, set_paths=None, conn=None):
        nonlocal pause_writes, adapter_writes
        if patch and patch.get("status") == SessionStatus.PAUSED.value:
            pause_writes += 1
            return None
        if patch and ("pending_park" in patch or "turn_no" in patch):
            adapter_writes += 1
        return await real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)

    monkeypatch.setattr(session_dispatch, "_noop_if_turn_already_completed", pause_between_guards)
    world.sessions.patch_if = refuse_both_pause_writes  # type: ignore[method-assign]
    pool = world.pool("wrk-cas-pause")
    pool._build_session_executor = lambda s: _async(_AskThenAnswer(world, s))  # type: ignore[method-assign]

    assert await world.claim_and_run(pool) == 1

    outcome = world.outcomes[-1]
    row = await world.row()
    assert pause_writes == 2
    assert outcome.entity_noop is True and outcome.requeue_after == timedelta(seconds=1)
    assert outcome.park is None and adapter_writes == 0
    assert row.status is SessionStatus.RUNNING and row.pause_requested is True
    assert row.parked_status is None and row.pending_park is not None
    assert len(world.llm_calls) == 1, "the entity-noop requeue must not run an executor or model"


@pytest.mark.asyncio
async def test_restart_clears_a_stash_left_by_lost_park_and_cancel_releases(world):
    """ENDED -> reopen stays on the same turn_no, so the old invocation's stash must be cleared explicitly."""
    await _leave_a_lost_park(world)
    requested = await world.sessions.patch_if(
        SID,
        {"cancel_requested": True},
        where={"status": [SessionStatus.RUNNING.value]},
    )
    assert requested is not None
    world.expire_lease()
    world.abandon_next_releases = 1
    assert await world.claim_and_run(world.pool("wrk-cancel")) == 1
    ended = await world.row()
    assert ended.status is SessionStatus.ENDED
    assert ended.pending_park is not None, "the cancelled outcome's release was abandoned too"
    assert ended.turn_no == 0

    world.expire_lease()
    await world.steer("restart")
    reopened = await world.row()
    assert reopened.pending_park is None
    assert reopened.turn_no == 0, "reopen deliberately does not bump the turn counter"
    pool = world.pool("wrk-restart")
    pool._build_session_executor = lambda s: _async(_AskThenAnswer(world, s))  # type: ignore[method-assign]
    assert await world.claim_and_run(pool) == 1

    assert len(world.llm_calls) == 2, "the new invocation must run instead of applying the prior invocation's park"
    assert world.outcomes[-1].park is None


@pytest.mark.parametrize(
    ("drained", "expected"),
    [
        pytest.param(None, None, id="null"),
        pytest.param(frozenset(), frozenset(), id="empty"),
        pytest.param(frozenset({"reply-a", "reply-b"}), frozenset({"reply-a", "reply-b"}), id="nonempty"),
    ],
)
def test_park_stash_round_trip_preserves_timestamps_and_drained_reply_keys(drained, expected):
    zone = timezone(timedelta(hours=5, minutes=30))
    parked_at = datetime(2026, 11, 6, 12, 34, 56, 789, tzinfo=zone)
    parked_until = parked_at + timedelta(minutes=7)
    park = ParkRequest(
        parked_state={"kind": "graph"},
        parked_event_key="graph:key",
        parked_event_keys=["graph:key", "graph:other"],
        parked_at=parked_at,
        parked_until=parked_until,
        drained_reply_keys=drained,
    )
    stash = {"turn_no": 4, "park": to_jsonable_python(asdict(park))}

    restored = session_dispatch._park_from_stash(stash)

    assert restored.parked_at == parked_at
    assert restored.parked_until == parked_until
    assert restored.drained_reply_keys == expected


@pytest.mark.asyncio
async def test_a_park_release_that_commits_leaves_no_stash(world):
    await world.create_session()
    await world.steer("first")
    pool = world.pool("wrk-a")

    async def build(session):
        return _AskThenAnswer(world, session)

    pool._build_session_executor = build  # type: ignore[method-assign]
    assert await world.claim_and_run(pool) == 1

    row = await world.row()
    assert row.parked_status == "parked"
    assert row.pending_park is None
    assert _applied() == 0


@pytest.mark.asyncio
async def test_a_stash_for_another_turn_is_ignored_and_the_turn_runs(world):
    """The stash names ONE turn. A row that has moved on (the release committed elsewhere) runs its turn, and the turn's release clears it."""
    await world.create_session()
    await world.steer("first")
    pool_a = world.pool("wrk-a")
    pool_a._build_session_executor = lambda s: _async(_AskThenAnswer(world, s))  # type: ignore[method-assign]
    world.abandon_next_releases = 1
    assert await world.claim_and_run(pool_a) == 1
    row = await world.row()
    assert row.pending_park is not None
    await world.sessions.update(row.model_copy(update={"turn_no": 1}))

    world.expire_lease()
    pool_b = world.pool("wrk-b")
    pool_b._build_session_executor = lambda s: _async(_AskThenAnswer(world, s))  # type: ignore[method-assign]
    assert await world.claim_and_run(pool_b) == 1

    assert len(world.llm_calls) == 2, "a stash for turn 0 must not be applied to turn 1"
    assert _applied() == 0
    assert (await world.row()).pending_park is None, "the turn's own release clears a stale stash"


@pytest.mark.asyncio
async def test_the_stash_is_fenced_on_the_turn_it_was_written_for(world):
    from primer.session.dispatch import _stash_pending_park

    row = await world.create_session(turn_no=3)
    park = ParkRequest(parked_state={}, parked_event_key="k", parked_until=None, parked_at=row.created_at)

    await _stash_pending_park(world.sessions, SID, 0, park)

    assert (await world.row()).pending_park is None, "the row has moved to another turn: no stash for the old one"


@pytest.mark.asyncio
async def test_a_stash_that_cannot_be_written_does_not_fail_the_park(world, caplog):
    await world.create_session()
    await world.steer("first")
    real_patch_if = world.sessions.patch_if

    async def patch_if(id, patch=None, *, where, set_paths=None, conn=None):
        if patch and "pending_park" in patch and patch["pending_park"] is not None:
            raise RuntimeError("storage down")
        return await real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)

    world.sessions.patch_if = patch_if  # type: ignore[method-assign]
    pool = world.pool("wrk-a")
    pool._build_session_executor = lambda s: _async(_AskThenAnswer(world, s))  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        assert await world.claim_and_run(pool) == 1

    row = await world.row()
    assert row.parked_status == "parked" and row.pending_park is None, "the park must still be applied by the release"
    assert any("stash" in r.getMessage() and SID in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_tool_wait_park_is_stashed_and_the_re_claim_applies_it():
    seen = await _tool_wait_park_release_abandoned_then_reclaimed()

    stash = seen["row_after_park_turn"].pending_park
    assert stash is not None and stash["turn_no"] == 0
    second = seen["second_outcome"]
    assert second.park is not None, "the re-claim must hand on the batch's park, not run the turn"
    assert second.park.parked_event_key == stash["park"]["parked_event_key"]
    assert seen["kinds_after_reclaim"].count(SessionMessageKind.TOOL_CALL.value) == 3
    assert seen["row_after_reclaim"].status is SessionStatus.RUNNING


async def _async(value):
    return value
