"""One turn per wake, third round (review round 2 of PR #877): the reviewer's probes P1-P7, asserting the FIXED behaviour.

* N1: a wake that took the turn (``arming``) settles it on EVERY exit: an early return after a reopen (the pause hold), a Cancel that won, an
  exception, a cancellation. Nothing leaves the row ``arming`` with no wake behind it.
* N2: a claim never consumes an ``arming`` row (it is handed back), and the session-messaging sweep heals a stale arm WHETHER OR NOT a lease row
  exists; a PAUSED row's stale arm goes back to idle.
* N3: the arm is stamped (``armed_at``) and the sweep ages it against a grace derived from the configured message write timeout; a live wake
  whose turn the sweep gave back while its message was being written takes it again and arms the claim: no message is left in the log of an
  idle, unclaimed session.
* N4: the drain's queued message leaves the queue once its USER_INPUT is written, before the turn is promoted: a turn that runs (and drains)
  before the realize returns never delivers it a second time.
* N5: the Cancel route writes only its own fields, fenced on the status it read: an arm made on another process keeps its ``last_seq``.
* N9: a pause flag that wins over a machine wake on a RUNNING row moves the row to PAUSED (as the worker does at a turn boundary), so
  ``/resume`` runs the turn that answers the message the wake wrote.

Every test runs on the suite's in-memory storage AND on a real SQLite storage (``new_world``), gives each wake its own lifecycle lock (two
processes share none), drives the interleaving with a hook at a named point (no sleeps), and is bounded by ``asyncio.timeout``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from primer.api.routers.sessions import pause_session, resume_session
from primer.bus.scheduler_tasks import SessionMessagingSweeper
from primer.int.claim import ClaimKind
from primer.model.chat import Done, TextDelta
from primer.model.storage import OffsetPage
from primer.model.workspace_session import PendingSessionMessage, SessionStatus, WorkspaceSession
from primer.session.dispatch import run_one_session_turn
from primer.session.pending_messages import realize_next_pending, store_pending_steer
from primer.session.steer_delivery import deliver_steer
from primer.workspace.session_factory import SessionCancelDeps, cancel_session
from tests.session._peer_fakes import FakeBus, FakeEngine, World, records, seed_session
from tests.session._wake_worlds import install_sessions, new_world  # noqa: F401  (new_world is a fixture)
from tests.session.test_dispatch import _make_lease
from tests.session.test_one_turn_per_wake import _AnsweringExecutor, _dispatch_deps, _each_wake_in_its_own_process

BOUND_S = 20.0
REQ = SimpleNamespace(state=SimpleNamespace(actor=None))


class _Engine(FakeEngine):
    """The world's claim engine, plus the lease drop the Cancel route makes."""

    async def delete_lease(self, kind: Any, session_id: str) -> None:
        self.leased.discard(session_id)


class _HookBus(FakeBus):
    """Notes every publish, and runs ONE action inline at the first publish of a key it was given."""

    def __init__(self) -> None:
        super().__init__()
        self.hooks: dict[str, Any] = {}

    async def publish(self, key: str, payload: dict) -> None:
        action = self.hooks.pop(key, None)
        if action is not None:
            await action()
        await super().publish(key, payload)


async def _world(new_world: Any, monkeypatch: Any) -> World:
    world = await new_world()
    world.bus = _HookBus()
    world.engine = _Engine()
    _each_wake_in_its_own_process(monkeypatch)
    ws = world.registry.workspace("ws-1")

    async def read_file(path: str) -> bytes:  # the log, as a workspace serves it to the sweep's IO shim
        return b"".join(ws.lines.get(path.split("/")[2], []))

    ws.read_file = read_file
    return world


def _at_user_input_write(world: World, action: Any) -> None:
    """Run ``action`` once, the moment a wake's USER_INPUT line reaches the workspace (after its arm, before the line lands)."""
    ws = world.registry.workspace("ws-1")
    real = ws.append_message_line
    state = {"done": False}

    async def append_message_line(session_id: str, line: bytes) -> None:
        if not state["done"] and b'"user_input"' in line:
            state["done"] = True
            await action()
        await real(session_id, line)

    ws.append_message_line = append_message_line


async def _row(world: World) -> WorkspaceSession:
    return await world.sessions.get("ses-b")


def _sweeper(world: World, grace: float | None = 0.0) -> SessionMessagingSweeper:
    return SessionMessagingSweeper(
        storage_provider=world.sp, scheduler=world.scheduler, claim_engine=world.engine, workspace_registry=world.registry,
        event_bus=world.bus, stale_arm_grace_seconds=grace,
    )


def _texts(world: World) -> list[str]:
    return [r["payload"]["text"] for r in records(world, "ses-b") if r["kind"] == "user_input"]


async def _queued(world: World) -> list[Any]:
    return list((await world.sp.get_storage(PendingSessionMessage).find(None, OffsetPage(offset=0, length=50))).items)


def _answering_deps(world: World, built: list[int]) -> Any:
    async def build(_row: Any) -> Any:
        built.append(1)
        return _AnsweringExecutor([TextDelta(text="answer", index=0), Done(stop_reason="stop", raw_reason="stop")])

    deps = _dispatch_deps(world, None)
    deps.build_executor = build
    return deps


# ---- N1 (P1): a machine wake that reopened an ENDED session and then met a pause gives its turn back ---------------------------------------


async def test_a_reopen_that_a_pause_overtakes_gives_the_turn_back_and_a_person_still_resumes_the_session(new_world, monkeypatch):
    async with asyncio.timeout(BOUND_S):
        world = await _world(new_world, monkeypatch)
        await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_status="idle", turn_no=1,
                           last_seq=4, next_unprocessed_seq=5)

        async def pause_on_another_process() -> None:
            await pause_session(workspace_id="ws-1", session_id="ses-b", sessions=world.sessions)   # CREATED -> PAUSED

        # the reopen publishes its divider tick between its closing write and the wake's arm
        world.bus.hooks["session:ses-b:tick"] = pause_on_another_process
        out = await deliver_steer(session_id="ses-b", text="from a trigger", parallelism="queue", human_intent=False, **world.deliver_kwargs())

        row = await _row(world)
        assert out.outcome == "queued"
        assert (row.status, row.turn_status, row.armed_at) == (SessionStatus.PAUSED, "idle", None), "the arm the reopen took was given back"
        assert [q.parts[0]["text"] for q in await _queued(world)] == ["from a trigger"]

        person = await deliver_steer(session_id="ses-b", text="a person", parallelism="queue", human_intent=True, **world.deliver_kwargs())

        assert person.outcome == "woken", "a person typing to a paused session resumes it (01a08c08)"
        row = await _row(world)
        assert (row.status, row.turn_status) == (SessionStatus.RUNNING, "claimable")


# ---- N2 (P2): a dead arm with a lease row is healed; a claim never consumes an arming row ---------------------------------------------------


async def test_a_dead_arm_that_a_lease_holds_is_handed_back_by_the_claim_and_healed_by_the_sweep(new_world, monkeypatch):
    async with asyncio.timeout(BOUND_S):
        world = await _world(new_world, monkeypatch)
        await seed_session(world, sid="ses-b", status=SessionStatus.WAITING, turn_status="idle", turn_no=1, last_seq=4, next_unprocessed_seq=5)
        at_write = asyncio.Event()

        async def resume_then_the_wake_process_dies() -> None:
            # /resume on another process arms a claim, then this wake's process is gone (no except runs): the wake never comes back
            await resume_session(REQ, workspace_id="ws-1", session_id="ses-b", sessions=world.sessions, scheduler=world.scheduler,
                                 engine=world.engine)
            world.engine.leased.add("ses-b")
            at_write.set()
            await asyncio.Event().wait()

        _at_user_input_write(world, resume_then_the_wake_process_dies)
        frozen = asyncio.create_task(
            deliver_steer(session_id="ses-b", text="hello", parallelism="queue", human_intent=True, **world.deliver_kwargs()),
        )
        try:
            await at_write.wait()
            assert (await _row(world)).turn_status == "arming"
            built: list[int] = []
            deps = _answering_deps(world, built)
            handed_back = await run_one_session_turn(_make_lease("ses-b"), deps)
            assert handed_back.entity_noop and handed_back.requeue_after is not None and built == [], "the claim does not consume the arm"

            # the arm is past its grace (0 here): the sweep heals it although a lease row exists; the log holds no message of it
            await _sweeper(world)._tick()

            row = await _row(world)
            assert (row.turn_status, row.armed_at) == ("idle", None)
            done = await run_one_session_turn(_make_lease("ses-b"), deps)
            assert done.success and built == [1], "the resumed session's claim runs now"
        finally:
            frozen.cancel()
            await asyncio.gather(frozen, return_exceptions=True)


async def test_the_sweep_heals_a_stale_arm_on_a_paused_session_back_to_idle(new_world, monkeypatch):
    async with asyncio.timeout(BOUND_S):
        world = await _world(new_world, monkeypatch)
        long_ago = datetime.now(UTC) - timedelta(hours=1)
        await seed_session(world, sid="ses-b", status=SessionStatus.PAUSED, turn_status="arming", armed_at=long_ago, turn_no=1, last_seq=4)

        await _sweeper(world, grace=None)._tick()

        row = await _row(world)
        assert (row.status, row.turn_status, row.armed_at) == (SessionStatus.PAUSED, "idle", None)
        assert world.engine.upserts == []


async def test_the_sweep_leaves_an_arm_younger_than_a_live_wake_s_worst_case(new_world, monkeypatch):
    """The grace is derived from the configured message write timeout (above a live wake's worst case): an arm stamped a moment ago is a live
    wake's, lease or no lease."""
    from primer.session.persistence import stale_arm_grace_seconds

    async with asyncio.timeout(BOUND_S):
        world = await _world(new_world, monkeypatch)
        assert stale_arm_grace_seconds() > 60
        await seed_session(world, sid="ses-b", status=SessionStatus.WAITING, turn_status="arming", armed_at=datetime.now(UTC), turn_no=1,
                           last_seq=4)

        await _sweeper(world, grace=None)._tick()

        assert (await _row(world)).turn_status == "arming"


# ---- N3 (P3, P3b): a heal under a live wake does not strand its message ---------------------------------------------------------------------


async def test_a_live_wake_whose_turn_the_sweep_gave_back_takes_it_again_and_arms_the_claim(new_world, monkeypatch):
    async with asyncio.timeout(BOUND_S):
        world = await _world(new_world, monkeypatch)
        await seed_session(world, sid="ses-b", status=SessionStatus.WAITING, turn_status="idle", turn_no=1, last_seq=4, next_unprocessed_seq=5)

        async def the_sweep_judges_the_wake_dead() -> None:
            # its USER_INPUT is not in the log yet, so the log says nothing is waiting and the heal gives the turn back
            assert await _sweeper(world)._heal_unclaimed_turns() == 1

        _at_user_input_write(world, the_sweep_judges_the_wake_dead)
        out = await deliver_steer(session_id="ses-b", text="slow write", parallelism="queue", human_intent=True, **world.deliver_kwargs())

        row = await _row(world)
        assert out.outcome == "woken"
        assert (row.turn_status, row.armed_at) == ("claimable", None)
        assert world.engine.upserts == [(ClaimKind.SESSION, "ses-b")]
        assert _texts(world) == ["slow write"]


async def test_a_realize_whose_turn_the_sweep_gave_back_still_runs_its_message_once(new_world, monkeypatch):
    async with asyncio.timeout(BOUND_S):
        world = await _world(new_world, monkeypatch)
        row = await seed_session(world, sid="ses-b", status=SessionStatus.WAITING, turn_status="idle", turn_no=1, last_seq=4,
                                 next_unprocessed_seq=5)
        await store_pending_steer(storage_provider=world.sp, session=row, text="queued earlier", workspace_registry=world.registry,
                                  event_bus=world.bus)

        async def the_sweep_judges_the_wake_dead() -> None:
            assert await _sweeper(world)._heal_unclaimed_turns() == 1

        _at_user_input_write(world, the_sweep_judges_the_wake_dead)
        woken = await realize_next_pending(storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", wake_deps=world.wake_deps())

        assert woken is True
        assert await _queued(world) == []
        assert (await _row(world)).turn_status == "claimable" and world.engine.upserts == [(ClaimKind.SESSION, "ses-b")]
        assert _texts(world) == ["queued earlier"]


# ---- N4 (P4): the queued row leaves the queue before the turn is promoted --------------------------------------------------------------------


async def test_a_turn_that_runs_before_the_realize_returns_never_gets_the_realized_message_twice(new_world, monkeypatch):
    async with asyncio.timeout(BOUND_S):
        world = await _world(new_world, monkeypatch)
        row = await seed_session(world, sid="ses-b", status=SessionStatus.WAITING, turn_status="idle", turn_no=1, last_seq=4,
                                 next_unprocessed_seq=5)
        await store_pending_steer(storage_provider=world.sp, session=row, text="only once", workspace_registry=world.registry,
                                  event_bus=world.bus)
        built: list[int] = []
        deps = _answering_deps(world, built)
        real_enqueue = world.scheduler.enqueue
        state = {"ran": False}

        async def enqueue(session_id: str) -> None:
            await real_enqueue(session_id)
            if not state["ran"]:
                # the realize promoted the turn and pulses the scheduler; a worker runs the whole turn, and its drain, before the realize
                # returns
                state["ran"] = True
                await run_one_session_turn(_make_lease("ses-b"), deps)

        world.scheduler.enqueue = enqueue
        assert await realize_next_pending(storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b",
                                          wake_deps=world.wake_deps()) is True

        assert built == [1]
        assert _texts(world) == ["only once"]
        assert await _queued(world) == []


# ---- N5 (P5): the Cancel route's write keeps an arm made on another process -----------------------------------------------------------------


async def test_a_cancel_whose_read_an_arm_overtakes_keeps_the_arms_seq_and_the_reopen_after_it_repeats_none(new_world, monkeypatch):
    async with asyncio.timeout(BOUND_S):
        world = await _world(new_world, monkeypatch)
        await seed_session(world, sid="ses-b", status=SessionStatus.WAITING, turn_status="idle", turn_no=1, last_seq=4, next_unprocessed_seq=5)
        real_sessions = world.sessions
        read_done = asyncio.Event()
        go = asyncio.Event()

        class _GatedSessions:
            """The Cancel route's view of the session storage: its write waits until the competing arm has landed."""

            def __getattr__(self, name: str) -> Any:
                return getattr(real_sessions, name)

            async def patch_if(self, *args: Any, **kw: Any) -> Any:
                read_done.set()
                await go.wait()
                return await real_sessions.patch_if(*args, **kw)

        class _Provider:
            def get_storage(self, cls: Any) -> Any:
                return _GatedSessions() if cls is WorkspaceSession else world.sp.get_storage(cls)

        cancel = asyncio.create_task(cancel_session(workspace_id="ws-1", session_id="ses-b", deps=SessionCancelDeps(
            storage_provider=_Provider(), scheduler=world.scheduler, claim_engine=world.engine, event_bus=None,
            workspace_registry=world.registry,
        )))
        await read_done.wait()

        async def the_cancel_lands() -> None:
            go.set()
            await cancel

        # a trigger's message on a worker pod arms the turn (last_seq 5) between the cancel's read and its write
        _at_user_input_write(world, the_cancel_lands)
        await deliver_steer(session_id="ses-b", text="armed at 5", parallelism="queue", human_intent=False, **world.deliver_kwargs())

        row = await _row(world)
        assert (row.status, row.ended_reason, row.last_seq, row.turn_status) == (SessionStatus.ENDED, "cancelled", 5, "idle"), (
            "the cancel ends the session without putting the arm's seq back; the wake gives its turn back"
        )

        await deliver_steer(session_id="ses-b", text="reopen", parallelism="queue", human_intent=True, **world.deliver_kwargs())
        seqs = [r["seq"] for r in records(world, "ses-b")]
        assert len(seqs) == len(set(seqs)), f"a seq was written twice: {seqs}"
        assert [r["kind"] for r in records(world, "ses-b")] == ["user_input", "invocation_divider", "user_input"]


# ---- N9 (P7): a pause that wins over a machine wake on a RUNNING row leaves a session /resume can run -----------------------------------------


async def test_a_pause_that_wins_over_a_machine_wake_on_a_running_row_pauses_it_and_resume_runs_the_message(new_world, monkeypatch):
    async with asyncio.timeout(BOUND_S):
        world = await _world(new_world, monkeypatch)
        await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="idle", turn_no=1, last_seq=4, next_unprocessed_seq=5)

        async def pause_on_another_process() -> None:
            await pause_session(workspace_id="ws-1", session_id="ses-b", sessions=world.sessions)   # RUNNING: pause_requested = True

        _at_user_input_write(world, pause_on_another_process)
        await deliver_steer(session_id="ses-b", text="from a trigger", parallelism="queue", human_intent=False, **world.deliver_kwargs())

        row = await _row(world)
        assert (row.status, row.turn_status, row.pause_requested) == (SessionStatus.PAUSED, "idle", True), (
            "no turn will come to honour the flag, so the row is paused as the worker pauses it at a turn boundary"
        )

        await resume_session(REQ, workspace_id="ws-1", session_id="ses-b", sessions=world.sessions, scheduler=world.scheduler,
                             engine=world.engine)

        row = await _row(world)
        assert (row.status, row.pause_requested) == (SessionStatus.RUNNING, False)
        assert world.engine.upserts == [(ClaimKind.SESSION, "ses-b")] and world.scheduler.enqueued == ["ses-b"]
        lines = [json.loads(line) for blob in world.registry.workspace("ws-1").lines["ses-b"] for line in blob.decode().splitlines() if line]
        assert [r["payload"]["text"] for r in lines if r["kind"] == "user_input"] == ["from a trigger"], "the turn /resume runs answers it"
