"""One turn per wake, second round (review of the G7-03 fix): the interleavings the first round left open.

* H1: a wake that decided the turn used to flip it ``claimable`` BEFORE its USER_INPUT was written, so a release that re-armed the claim, boot
  recovery or a claim armed by ``/resume`` could start a turn with nothing new and then a second one. The wake now takes the turn as ``arming``:
  busy to every other wake, ignored by the re-arm and by boot recovery, handed back by a claim; it is promoted to ``claimable`` only once the
  message is written, and never over a turn another writer started.
* H2: reopening an ENDED session was a whole-row write from the row read before the lock (two reopens on two processes both landed, the second
  putting ``last_seq`` back), and every terminal block wrote ENDED before the turn's ``last_seq``. The reopen now decides with one fenced write that
  reserves the divider's seq, and the turn's records are on the row before its status moves.
* M3: no path leaves a turn armed with nothing to run or no claim: a failed write or closing write gives the turn back, the claim is armed even when
  the scheduler's pulse fails, and the session-messaging sweep heals a turn left ``claimable`` with no lease or ``arming`` with no wake behind it.
* M6: a wake no person stands behind never clears a pause requested on another process since its decision.

Every test gives each wake its own lifecycle lock (two processes share none) and runs the real delivery, wake, dispatch, re-arm, boot recovery and
sweep over the suite's in-memory storage, whose ``patch_if`` honours its predicate. The competing writer runs at the exact instant it names (inside
the wake's USER_INPUT write, at the turn's status transition). Every body is bounded by ``asyncio.timeout``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from primer.api._app_lifespan_phases import recover_sessions
from primer.bus.scheduler_tasks import SessionMessagingSweeper
from primer.int.claim import ClaimKind
from primer.model.chat import Done, TextDelta
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.dispatch import run_one_session_turn
from primer.session.steer_delivery import deliver_steer
from primer.worker.pool import WorkerPool
from tests.session._peer_fakes import World, make_world, records, seed_session
from tests.session.test_dispatch import _make_lease
from tests.session.test_one_turn_per_wake import (
    _AnsweringExecutor,
    _armed_turn_world,
    _assert_seqs_unique,
    _CancelBeforeTheLastEvent,
    _dispatch_deps,
    _each_wake_in_its_own_process,
    _pending_texts,
    _route_both_then_race,
    _user_inputs,
)

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
BOUND_S = 15.0


def _while_the_message_is_written(world: World, action: Any, sid: str = "ses-b") -> None:
    """Run ``action`` once, the moment a wake's USER_INPUT write reaches the workspace: after its decision, before its message lands."""
    ws = world.registry.workspace("ws-1")
    real = ws.append_message_line
    state = {"done": False}

    async def append_message_line(session_id: str, line: bytes) -> None:
        if not state["done"] and session_id == sid and b'"user_input"' in line:
            state["done"] = True
            await action()
        await real(session_id, line)

    ws.append_message_line = append_message_line


async def _steer(world: World, text: str = "the message", *, human: bool = True):
    return await deliver_steer(session_id="ses-b", text=text, parallelism="queue", human_intent=human, **world.deliver_kwargs())


async def _row(world: World) -> WorkspaceSession:
    return await world.sessions.get("ses-b")


def _kinds(world: World) -> list[str]:
    return [r["kind"] for r in records(world, "ses-b")]


# ---- H1: the arm is a state nothing claims ------------------------------------------------------------------------------------------------


async def test_a_release_that_rearms_while_a_wake_writes_its_message_arms_no_claim(monkeypatch):
    """The pool's re-arm after a release (``WorkerPool._maybe_rearm_session``, the real method) reads the row while a wake on another process is
    writing its message. It used to find ``claimable`` and arm a claim for a turn whose message was not there yet."""
    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        _each_wake_in_its_own_process(monkeypatch)
        await seed_session(world, sid="ses-b", turn_no=1, created_at=T0)
        pool = SimpleNamespace(_storage=world.sp, _engine=world.engine)
        seen = []

        async def release_rearms() -> None:
            seen.append((await _row(world)).turn_status)
            await WorkerPool._maybe_rearm_session(pool, "ses-b")

        _while_the_message_is_written(world, release_rearms)
        out = await _steer(world)

        assert out.outcome == "woken"
        assert seen == ["arming"]
        assert world.engine.upserts == [(ClaimKind.SESSION, "ses-b")], "one claim, armed by the wake once its message was written"
        assert (await _row(world)).turn_status == "claimable"


async def test_a_claim_that_reaches_a_turn_a_wake_is_arming_is_handed_back_and_the_turn_runs_once_with_the_message(monkeypatch):
    """A claim armed by ``/resume`` or boot recovery reaches the session while a wake writes its message: the real turn hands the claim back
    untouched (no executor built, no model call, nothing written to the row) instead of answering without the message. Once the wake has promoted
    the turn, the claim comes round again and the ONE turn runs, after the message."""
    async with asyncio.timeout(BOUND_S):
        world = await _armed_turn_world()
        try:
            _each_wake_in_its_own_process(monkeypatch)
            await world.sessions.patch_if("ses-b", {"turn_status": "idle", "status": "waiting"}, where={"turn_status": ["claimable"]})
            built: list[int] = []

            async def build(_row: Any) -> Any:
                built.append(1)
                return _AnsweringExecutor([TextDelta(text="the answer", index=0), Done(stop_reason="stop", raw_reason="stop")])

            deps = _dispatch_deps(world, None)
            deps.build_executor = build
            early: list[Any] = []

            async def a_claim_lands() -> None:
                early.append(await run_one_session_turn(_make_lease("ses-b"), deps))

            _while_the_message_is_written(world, a_claim_lands)
            out = await _steer(world)

            assert out.outcome == "woken"
            (handed_back,) = early
            assert handed_back.entity_noop and handed_back.requeue_after is not None and not handed_back.success
            assert built == [], "no turn ran while the message was being written"
            assert (await _row(world)).turn_status == "claimable"

            done = await run_one_session_turn(_make_lease("ses-b"), deps)

            assert done.success and built == [1]
            kinds = _kinds(world)
            assert kinds.index("user_input") < kinds.index("done"), kinds
            _assert_seqs_unique(world)
        finally:
            await world.bus.aclose()


async def test_boot_recovery_leaves_a_turn_a_wake_is_arming_to_that_wake():
    """Boot recovery re-arms every RUNNING row. A row a wake on another process (one that did not restart) is arming is that wake's to arm."""
    async with asyncio.timeout(BOUND_S):
        world = make_world()
        await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="arming", turn_no=1, created_at=T0)
        await seed_session(world, sid="ses-c", status=SessionStatus.RUNNING, turn_no=1, created_at=T0)

        await recover_sessions(world.engine, world.scheduler, world.sp)

        assert world.engine.upserts == [(ClaimKind.SESSION, "ses-c")]


async def test_a_routed_wake_never_writes_claimable_over_a_turn_another_writer_started(monkeypatch):
    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        _each_wake_in_its_own_process(monkeypatch)
        await seed_session(world, sid="ses-b", turn_no=1, created_at=T0)

        async def a_turn_starts() -> None:
            await world.sessions.patch_if("ses-b", {"turn_status": "running"}, where={"turn_status": ["arming", "claimable"]})

        _while_the_message_is_written(world, a_turn_starts)
        await _steer(world)

        assert (await _row(world)).turn_status == "running"
        assert world.engine.upserts == []


# ---- H2: the reopen is fenced, and the turn's records are on the row before it ends --------------------------------------------------------


async def test_two_wakes_in_two_processes_that_both_read_the_session_ended_reopen_it_once(monkeypatch):
    """Both read ENDED. One reopens it (one divider at the seq its decision reserved) and starts the turn; the other's decision is refused, it
    reads the turn armed and queues its message. Before, both wrote a divider at the same seq, and the second's whole-row reopen put ``last_seq``
    back, so its USER_INPUT repeated the first's seq."""
    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        _each_wake_in_its_own_process(monkeypatch)
        await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_no=2, last_seq=3,
                           metadata={"invocation": 1}, created_at=T0)
        _route_both_then_race(monkeypatch, world, "ses-b")

        outcomes = await asyncio.gather(*(_steer(world, text) for text in ("first", "second")))

        assert sorted(o.outcome for o in outcomes) == ["queued", "woken"]
        assert _kinds(world) == ["invocation_divider", "user_input"]
        assert [r["seq"] for r in records(world, "ses-b")] == [4, 5]
        assert len(await _pending_texts(world)) == 1
        row = await _row(world)
        assert (row.status, row.turn_status, row.last_seq, row.metadata["invocation"]) == (SessionStatus.RUNNING, "claimable", 5, 2)


class _OnEnded:
    """The real session storage, plus a hook that runs ONCE, inline, the moment a write turns the row ENDED: the instant from which a wake on
    another process sees it ended. Everything else goes straight through."""

    def __init__(self, inner: Any, session_id: str, action: Any) -> None:
        self._inner = inner
        self._sid = session_id
        self._action = action
        self.fired = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def _watch(self, sid: str, write: Any) -> Any:
        before = await self._inner.get(sid)
        out = await write()
        after = await self._inner.get(sid)
        if (
            not self.fired and sid == self._sid and before is not None and after is not None
            and before.status != SessionStatus.ENDED and after.status == SessionStatus.ENDED
        ):
            self.fired = True
            await self._action()
        return out

    async def update(self, entity: Any, **kw: Any) -> Any:
        return await self._watch(entity.id, lambda: self._inner.update(entity, **kw))

    async def update_unless(self, entity: Any, **kw: Any) -> Any:
        return await self._watch(entity.id, lambda: self._inner.update_unless(entity, **kw))

    async def patch_if(self, id: str, patch: Any = None, **kw: Any) -> Any:  # noqa: A002
        return await self._watch(id, lambda: self._inner.patch_if(id, patch, **kw))


async def test_cancel_then_type_straight_away_reuses_no_seq_and_answers_the_message_after_the_cancel(monkeypatch):
    """A person cancels a turn and types at once; the steer lands on another process the moment the row reads ENDED. Before, the cancelled exit
    wrote ENDED before the turn's ``last_seq``: the reopen read a stale ``last_seq``, so its divider and USER_INPUT repeated seqs the turn had
    written (the CANCELLED record among them). Now ``last_seq`` is on the row first; the turn still holds its claim at that instant, so the
    steer is queued and the drain of the cancelled exit reopens the session with it, after the CANCELLED record."""
    async with asyncio.timeout(BOUND_S):
        world = await _armed_turn_world()
        try:
            _each_wake_in_its_own_process(monkeypatch)
            world.engine.leased.add("ses-b")              # the turn holds its claim until its release
            deliveries = []

            async def type_straight_away() -> None:
                deliveries.append(await _steer(world, "never mind, do this instead"))

            hook = _OnEnded(world.sessions, "ses-b", type_straight_away)
            world.sp._stores[WorkspaceSession] = hook

            await run_one_session_turn(_make_lease("ses-b"), _dispatch_deps(world, _CancelBeforeTheLastEvent(world.sessions, "ses-b")))

            assert hook.fired and [d.outcome for d in deliveries] == ["queued"]
            seqs = _assert_seqs_unique(world)
            assert seqs == sorted(seqs)
            kinds = _kinds(world)
            assert kinds[-3:] == ["cancelled", "invocation_divider", "user_input"], kinds
            assert [r["payload"]["text"] for r in _user_inputs(world)] == ["never mind, do this instead"]
            row = await _row(world)
            assert (row.status, row.turn_status, row.last_seq) == (SessionStatus.RUNNING, "claimable", max(seqs))
        finally:
            await world.bus.aclose()


# ---- M3: no turn is left armed with nothing to run, or with no claim ------------------------------------------------------------------------


async def test_a_scheduler_pulse_that_fails_still_arms_the_claim(monkeypatch):
    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        _each_wake_in_its_own_process(monkeypatch)
        await seed_session(world, sid="ses-b", turn_no=1, created_at=T0)

        async def enqueue(session_id: str) -> None:
            raise RuntimeError("the scheduler's notify failed")

        world.scheduler.enqueue = enqueue
        with pytest.raises(RuntimeError):
            await _steer(world)

        assert (await _row(world)).turn_status == "claimable"
        assert world.engine.upserts == [(ClaimKind.SESSION, "ses-b")], "the claim is armed whatever the pulse did"


async def test_a_closing_write_that_fails_gives_the_turn_back(monkeypatch):
    """The wake's message is written and its closing write (the promotion to claimable) fails: the turn goes back to idle, not left armed with
    no claim for every later message to queue behind."""
    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        _each_wake_in_its_own_process(monkeypatch)
        await seed_session(world, sid="ses-b", turn_no=1, created_at=T0)
        sessions = world.sessions
        real = sessions.patch_if

        async def patch_if(id: str, patch: Any = None, **kw: Any) -> Any:  # noqa: A002
            if "pause_requested" in (patch or {}):          # the closing write, the one that clears the pause flags
                raise ConnectionError("the database went away")
            return await real(id, patch, **kw)

        sessions.patch_if = patch_if
        with pytest.raises(ConnectionError):
            await _steer(world)

        assert (await _row(world)).turn_status == "idle"
        assert world.engine.upserts == []


async def test_a_sender_stopped_mid_delivery_gives_the_turn_back(monkeypatch):
    """A peer message's sender is stopped (its task cancelled) after the message is written, during the wake's closing write: the turn its wake
    took goes back to idle instead of staying armed with no claim."""
    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        _each_wake_in_its_own_process(monkeypatch)
        await seed_session(world, sid="ses-b", turn_no=1, created_at=T0)
        writing = asyncio.Event()
        sessions = world.sessions
        real = sessions.patch_if

        async def patch_if(id: str, patch: Any = None, **kw: Any) -> Any:  # noqa: A002
            if "pause_requested" in (patch or {}):          # the closing write
                writing.set()
                await asyncio.Event().wait()
            return await real(id, patch, **kw)

        sessions.patch_if = patch_if
        delivery = asyncio.create_task(_steer(world, human=False))
        await writing.wait()
        delivery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await delivery
        sessions.patch_if = real

        assert (await _row(world)).turn_status == "idle"
        assert world.engine.upserts == []


def _sweeper(world: World, *, grace: float = 0.0) -> SessionMessagingSweeper:
    return SessionMessagingSweeper(
        storage_provider=world.sp, scheduler=world.scheduler, claim_engine=world.engine, workspace_registry=world.registry,
        stale_arm_grace_seconds=grace,
    )


def _log_is_readable(world: World) -> None:
    """The fake workspace serves the session's message log the way a workspace's ``read_file`` does (the sweep reads it through the IO shim)."""
    ws = world.registry.workspace("ws-1")

    async def read_file(path: str) -> bytes:
        assert path == ".state/sessions/ses-b/messages.jsonl", path
        return b"".join(ws.lines.get("ses-b", []))

    ws.read_file = read_file


async def test_the_sweep_arms_the_claim_of_a_turn_left_claimable_with_none(monkeypatch):
    """The wake promoted the turn and its claim upsert failed: nothing ran it, and every later message queued behind it."""
    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        _each_wake_in_its_own_process(monkeypatch)
        await seed_session(world, sid="ses-b", turn_no=1, created_at=T0)
        real = world.engine.upsert
        failed = {"once": False}

        async def upsert(*args: Any, **kw: Any) -> None:
            if not failed["once"]:
                failed["once"] = True
                raise ConnectionError("the lease table is unreachable")
            await real(*args, **kw)

        world.engine.upsert = upsert
        with pytest.raises(ConnectionError):
            await _steer(world)
        assert (await _row(world)).turn_status == "claimable" and world.engine.upserts == []

        await _sweeper(world)._tick()

        assert world.engine.upserts == [(ClaimKind.SESSION, "ses-b")]
        assert world.scheduler.enqueued[-1] == "ses-b"


async def test_the_sweep_heals_an_unclaimed_turn_only_after_it_stayed_so_for_the_grace(monkeypatch):
    """A row claimable with no lease for a moment is the normal instant between a promotion and its upsert: the first sighting only notes it."""
    async with asyncio.timeout(BOUND_S):
        world = make_world()
        await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="claimable", turn_no=1, created_at=T0)
        sweeper = _sweeper(world, grace=0.2)

        await sweeper._tick()
        assert world.engine.upserts == []
        await asyncio.sleep(0.25)
        await sweeper._tick()

        assert world.engine.upserts == [(ClaimKind.SESSION, "ses-b")]


@pytest.mark.parametrize("message_written", [True, False], ids=["its message was written", "its message never landed"])
async def test_the_sweep_heals_a_turn_left_arming_by_a_wake_that_died(message_written):
    """The wake's process died between its arm and its promotion. The log decides: a USER_INPUT nobody answered arms the turn and its claim; none
    gives the turn back."""
    async with asyncio.timeout(BOUND_S):
        world = make_world()
        await seed_session(world, sid="ses-b", status=SessionStatus.WAITING, turn_status="arming", turn_no=1, last_seq=5,
                           next_unprocessed_seq=5, created_at=T0)
        if message_written:
            line = {"seq": 5, "kind": "user_input", "payload": {"text": "hello"}, "created_at": T0.isoformat()}
            world.registry.workspace("ws-1").lines["ses-b"] = [(json.dumps(line) + "\n").encode()]
        _log_is_readable(world)

        assert await _sweeper(world)._heal_unclaimed_turns() == 1

        row = await _row(world)
        if message_written:
            assert row.turn_status == "claimable" and world.engine.upserts == [(ClaimKind.SESSION, "ses-b")]
        else:
            assert row.turn_status == "idle" and world.engine.upserts == []


async def test_the_sweep_leaves_a_turn_a_claim_holds():
    async with asyncio.timeout(BOUND_S):
        world = make_world()
        world.engine.leased.add("ses-b")
        await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="claimable", turn_no=1, created_at=T0)

        assert await _sweeper(world)._heal_unclaimed_turns() == 0
        assert world.engine.upserts == []


# ---- M6: a pause requested on another process wins over a machine wake ---------------------------------------------------------------------


async def test_a_pause_requested_on_another_process_while_a_machine_wake_writes_is_not_cleared(monkeypatch):
    """A trigger's message (no person behind it) to an idle RUNNING session; a pause is requested on another process while the message is being
    written. The wake used to clear the flag with its closing write. Now the pause wins: the flag stays, the turn goes back to idle and is not
    claimed; the message is in the log for the turn that runs after the resume."""
    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        _each_wake_in_its_own_process(monkeypatch)
        await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_no=1, created_at=T0)

        async def pause() -> None:
            await world.sessions.patch_if("ses-b", {"pause_requested": True}, where={"status": ["running"]})

        _while_the_message_is_written(world, pause)
        await _steer(world, "from a trigger", human=False)

        row = await _row(world)
        assert (row.pause_requested, row.turn_status) == (True, "idle")
        assert world.engine.upserts == []
        assert [r["payload"]["text"] for r in _user_inputs(world)] == ["from a trigger"]


async def test_a_person_s_message_still_resumes_over_a_pause_requested_since(monkeypatch):
    """The fence is for a wake no person stands behind: a person typing while paused means resume (01a08c08), as before."""
    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        _each_wake_in_its_own_process(monkeypatch)
        await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_no=1, created_at=T0)

        async def pause() -> None:
            await world.sessions.patch_if("ses-b", {"pause_requested": True}, where={"status": ["running"]})

        _while_the_message_is_written(world, pause)
        await _steer(world, "from a person", human=True)

        row = await _row(world)
        assert (row.pause_requested, row.turn_status) == (False, "claimable")
