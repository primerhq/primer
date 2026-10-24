"""One turn per wake (audit G7-03): wakes of one idle session arm ONE turn whatever process each runs in, a message never overtakes an older
queued one, and a USER_INPUT never repeats a seq the ending turn already wrote.

The guarantee is in the data. A routed wake decides the open turn with ONE conditional write of the session row (``turn_status`` idle ->
claimable, fenced on the ``last_seq`` it read, which the same write reserves for its USER_INPUT) BEFORE it writes the USER_INPUT. The process-local
``session_lifecycle_lock`` stays an optimisation: every test here gives each wake its OWN lock, as two processes (the API pod and a worker pod) have.

The tests run the real ``deliver_steer``, ``send_peer_message``, ``wake_session``, ``store_pending_steer``, ``realize_next_pending`` and
``run_one_session_turn`` over the suite's in-memory storage, whose ``patch_if`` evaluates its predicate against the stored row
(``tests/storage/_patch_reference.py``). Nothing on those paths is stubbed: a test only adds a seam where it names the interleaving it forces (a
barrier at the wake's own read, a hook at the moment a turn's row turns idle). Every body is bounded by ``asyncio.timeout``, so no interleaving can
hang the lane.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.model.chat import Done, TextDelta
from primer.model.principal import PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.workspace_session import AgentSessionBinding, PendingSessionMessage, SessionStatus, WorkspaceSession
from primer.session import enqueue as enqueue_mod
from primer.session import steer_delivery
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.session.mutation_lock import KeyedLock
from primer.session.peer_messages import send_peer_message
from primer.session.pending_messages import realize_next_pending, store_pending_steer
from primer.session.steer_delivery import deliver_steer
from tests.session._peer_fakes import World, make_world, records, seed_session
from tests.session.test_dispatch import FakeExecutor, _make_lease

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
USER = PrincipalRef(type="user", id="u1", display="u1", role="user", source="local")
PEER = {"peer": {"from_session_id": "ses-a", "from_workspace_id": "ws-1", "hops": 1}}
BOUND_S = 15.0


def _each_wake_in_its_own_process(monkeypatch) -> None:
    """Every ``wake_session`` call takes a lifecycle lock of its own, so no two wakes serialise on it: two processes share no lock."""
    monkeypatch.setattr(enqueue_mod, "session_lifecycle_lock", lambda: KeyedLock())


def _user_inputs(world: World, sid: str = "ses-b") -> list[dict[str, Any]]:
    return [r for r in records(world, sid) if r["kind"] == "user_input"]


async def _pending_texts(world: World, sid: str = "ses-b") -> list[str]:
    page = await world.sp.get_storage(PendingSessionMessage).find(None, OffsetPage(offset=0, length=200))
    rows = sorted((r for r in page.items if r.session_id == sid), key=lambda r: (r.enqueued_at, r.id))
    return [r.parts[0]["text"] for r in rows]


def _assert_seqs_unique(world: World, sid: str = "ses-b") -> list[int]:
    seqs = [r["seq"] for r in records(world, sid)]
    assert len(seqs) == len(set(seqs)), f"a seq was written twice: {seqs}"
    return seqs


class _ReadBarrier:
    """The real session storage; once ``armed``, the first ``parties`` reads of ``session_id`` wait for each other, so every wake has READ the row
    before any of them writes (what two processes reading one idle row at the same moment do). Every other call goes straight through."""

    def __init__(self, inner: Any, session_id: str, parties: int = 2) -> None:
        self._inner = inner
        self._sid = session_id
        self._barrier = asyncio.Barrier(parties)
        self._parties = parties
        self._arrived = 0
        self.armed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def get(self, id: str, **kw: Any) -> Any:  # noqa: A002
        row = await self._inner.get(id, **kw)
        if self.armed and id == self._sid and self._arrived < self._parties:
            self._arrived += 1
            try:
                # bounded: a path that never makes the second read (it did not get this far) is answered by the assertions, not a hang
                await asyncio.wait_for(self._barrier.wait(), timeout=2.0)
            except TimeoutError:
                pass
        return row


def _route_both_then_race(monkeypatch, world: World, sid: str) -> _ReadBarrier:
    """Both deliveries route the idle row first (``deliver_steer``'s read), then both wakes read it again inside their own locks before either
    writes. On main both then wrote a USER_INPUT; the conditional arm lets exactly one through."""
    barrier = _ReadBarrier(world.sp.get_storage(WorkspaceSession), sid)
    world.sp._stores[WorkspaceSession] = barrier
    real_wake = steer_delivery.wake_session
    entered = asyncio.Barrier(2)

    async def wake_together(**kwargs: Any) -> Any:
        await entered.wait()
        barrier.armed = True
        return await real_wake(**kwargs)

    monkeypatch.setattr(steer_delivery, "wake_session", wake_together)
    return barrier


# ---- (a) two wakes in two processes ------------------------------------------------------------------------------------------------------


async def test_two_wakes_in_two_processes_that_both_read_the_target_idle_arm_one_turn(monkeypatch):
    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        await seed_session(world, sid="ses-b", turn_no=1, last_seq=4, created_at=T0)
        _each_wake_in_its_own_process(monkeypatch)
        _route_both_then_race(monkeypatch, world, "ses-b")

        outcomes = await asyncio.gather(*(
            deliver_steer(session_id="ses-b", text=text, parallelism="queue", human_intent=True, **world.deliver_kwargs())
            for text in ("first", "second")
        ))

        assert sorted(o.outcome for o in outcomes) == ["queued", "woken"]
        (armed,) = _user_inputs(world)
        (waiting,) = await _pending_texts(world)
        assert {armed["payload"]["text"], waiting} == {"first", "second"}, "both messages are kept, one per turn"
        assert armed["seq"] == 5, "the USER_INPUT takes the seq its arm reserved"
        row = await world.sessions.get("ses-b")
        assert (row.turn_status, row.status, row.last_seq) == ("claimable", SessionStatus.RUNNING, 5)
        assert world.scheduler.enqueued == ["ses-b"], "one turn is armed, once"


async def test_two_wakes_that_both_read_the_target_idle_skip_the_second_for_if_idle(monkeypatch):
    """``parallelism="skip"`` (``if_idle``): the wake that loses the arm reports ``skipped_busy`` and stores nothing."""
    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        await seed_session(world, sid="ses-b", turn_no=1, created_at=T0)
        _each_wake_in_its_own_process(monkeypatch)
        _route_both_then_race(monkeypatch, world, "ses-b")

        outcomes = await asyncio.gather(*(
            deliver_steer(session_id="ses-b", text=text, parallelism="skip", human_intent=False, **world.deliver_kwargs())
            for text in ("first", "second")
        ))

        assert sorted(o.outcome for o in outcomes) == ["skipped_busy", "woken"]
        assert len(_user_inputs(world)) == 1
        assert await _pending_texts(world) == []


async def test_the_audit_shape_two_peer_messages_with_one_lock_each_start_one_turn(monkeypatch):
    """The audit's reproduction of G7-03 (a) (scratch ``test_fc1``): the #821 scenario with one ``KeyedLock`` per wake."""
    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        await seed_session(world, sid="ses-a", binding=AgentSessionBinding(agent_id="planner"), initiated_by=USER, created_at=T0)
        await seed_session(world, sid="ses-c", binding=AgentSessionBinding(agent_id="reviewer"), initiated_by=USER,
                           status=SessionStatus.RUNNING, turn_status="running", turn_no=1, created_at=T0)
        await seed_session(world, sid="ses-b", turn_no=1, created_at=T0)
        _each_wake_in_its_own_process(monkeypatch)
        real_wake = steer_delivery.wake_session
        both_routed = asyncio.Barrier(2)

        async def wake_together(**kwargs: Any) -> Any:
            await both_routed.wait()
            return await real_wake(**kwargs)

        monkeypatch.setattr(steer_delivery, "wake_session", wake_together)
        kw = dict(world.deliver_kwargs(), target_session_id="ses-b")
        outcomes = await asyncio.gather(
            send_peer_message(sender_session_id="ses-a", sender_principal=USER, text="first", **kw),
            send_peer_message(sender_session_id="ses-c", sender_principal=USER, text="second", **kw),
        )

        assert sorted(o.delivery for o in outcomes) == ["queued", "woken"]
        assert len(_user_inputs(world)) == 1
        assert len(await _pending_texts(world)) == 1


# ---- (b) the queue is first in, first out -------------------------------------------------------------------------------------------------


async def test_a_message_that_lands_while_an_older_one_is_queued_goes_behind_it():
    """The audit's reproduction of G7-03 (b) (scratch ``test_fc2``): the turn ended and a peer message is still queued when a person's steer
    lands. The steer is queued behind it, and the drain's realize then starts the turn with the OLDER message: one USER_INPUT for the turn."""
    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        row = await seed_session(world, sid="ses-b", turn_no=1, turn_status="idle", created_at=T0)
        await store_pending_steer(storage_provider=world.sp, session=row, text="older queued peer message",
                                  workspace_registry=world.registry, event_bus=world.bus, attribution=PEER)

        first = await deliver_steer(session_id="ses-b", text="newer person message", parallelism="queue", human_intent=True,
                                    **world.deliver_kwargs())
        assert first.outcome == "queued"
        assert _user_inputs(world) == []

        assert await realize_next_pending(storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b",
                                          wake_deps=world.wake_deps()) is True
        assert [r["payload"]["text"] for r in _user_inputs(world)] == ["older queued peer message"]
        assert (await world.sessions.get("ses-b")).turn_status == "claimable"
        assert await _pending_texts(world) == ["newer person message"], "the newer message waits for the next turn boundary"


async def test_a_peer_message_to_an_idle_session_with_older_messages_waiting_is_queued_behind_them_and_says_so():
    """The sending agent is told what became of its message: queued behind the older ones (not "in a turn"), and the events say it was steered."""
    from primer.session.peer_messages import NOTE_QUEUED_BEHIND

    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        await seed_session(world, sid="ses-a", binding=AgentSessionBinding(agent_id="planner"), initiated_by=USER,
                           status=SessionStatus.RUNNING, turn_status="running", turn_no=1, created_at=T0)
        row = await seed_session(world, sid="ses-b", turn_no=1, turn_status="idle", created_at=T0)
        await store_pending_steer(storage_provider=world.sp, session=row, text="older", workspace_registry=world.registry, event_bus=world.bus)

        result = await send_peer_message(sender_session_id="ses-a", sender_principal=USER, target_session_id="ses-b", text="newer",
                                         **world.deliver_kwargs())

        assert (result.delivery, result.note) == ("queued", NOTE_QUEUED_BEHIND)
        assert _user_inputs(world) == []
        assert await _pending_texts(world) == ["older", "[from session ses-a (agent planner)]\nnewer"]
        steered = await world.sp.get_event_store().read_after(0, event_type_prefix="session.steered")
        assert [e.payload["delivery"] for e in steered] == ["queued"]


async def test_a_realize_that_finds_the_turn_already_armed_leaves_the_message_queued_where_it_is(monkeypatch):
    """Another wake armed the turn after the turn ended (a wake on another process, whose queue read came first): the realize must not write a
    second USER_INPUT for that turn. Its message stays at the head of the queue for the next boundary, the same row, not a copy."""
    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        _each_wake_in_its_own_process(monkeypatch)
        row = await seed_session(world, sid="ses-b", turn_no=1, turn_status="idle", created_at=T0)
        queued = await store_pending_steer(storage_provider=world.sp, session=row, text="older", workspace_registry=world.registry,
                                           event_bus=world.bus)
        # the other wake: it armed the turn and wrote its USER_INPUT
        await enqueue_mod.wake_session(workspace_id="ws-1", session_id="ses-b", instruction="armed first", human_intent=True,
                                       deps=world.wake_deps())

        assert await realize_next_pending(storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b",
                                          wake_deps=world.wake_deps()) is False
        assert [r["payload"]["text"] for r in _user_inputs(world)] == ["armed first"]
        page = await world.sp.get_storage(PendingSessionMessage).find(None, OffsetPage(offset=0, length=10))
        assert [p.id for p in page.items] == [queued.id]


async def test_two_realizes_of_one_queue_head_in_two_processes_deliver_it_once(monkeypatch):
    """Two drains of the same queue (a turn's checkpoint and the sweep, say) both read the same head and the idle row: one arms the turn with it,
    the other leaves it, and the message is delivered once."""
    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        _each_wake_in_its_own_process(monkeypatch)
        row = await seed_session(world, sid="ses-b", turn_no=1, created_at=T0)
        await store_pending_steer(storage_provider=world.sp, session=row, text="only once", workspace_registry=world.registry,
                                  event_bus=world.bus)
        barrier = _ReadBarrier(world.sp.get_storage(WorkspaceSession), "ses-b")
        world.sp._stores[WorkspaceSession] = barrier
        barrier.armed = True

        done = await asyncio.gather(*(
            realize_next_pending(storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", wake_deps=world.wake_deps())
            for _ in range(2)
        ))

        assert sorted(done) == [False, True]
        assert [r["payload"]["text"] for r in _user_inputs(world)] == ["only once"]
        assert await _pending_texts(world) == []


# ---- (c) and the realize window, through the real dispatch --------------------------------------------------------------------------------


class _OnIdle:
    """The real session storage, plus a hook that runs ONCE, inline, the moment a write turns the row's ``turn_status`` from running to idle: the
    instant from which a wake on another process sees the row idle. Everything else goes straight through."""

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
            and before.turn_status == "running" and after.turn_status == "idle"
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


class _CancelBeforeTheLastEvent(FakeExecutor):
    """An executor whose session is cancelled (the row's ``cancel_requested``) after its text and before its ``Done``: the turn takes the cancelled
    exit for a Cancel found after the stream's last event."""

    def __init__(self, sessions: Any, sid: str) -> None:
        super().__init__([])
        self._sessions = sessions
        self._sid = sid

    async def invoke(self, messages: list[Any], **kwargs: Any):
        yield TextDelta(text="partial answer", index=0)
        await self._sessions.patch_if(self._sid, {"cancel_requested": True}, where={"cancel_requested": [False]})
        yield Done(stop_reason="stop", raw_reason="stop")


class _AnsweringExecutor(FakeExecutor):
    """A turn that answers and stops, as a real agent executor reports it (``last_done_reason``): an interactive session comes to rest WAITING."""

    last_done_reason = "stop"


def _exit_executor(world: World, exit_kind: str) -> Any:
    if exit_kind == "clean":
        return _AnsweringExecutor([TextDelta(text="the answer", index=0), Done(stop_reason="stop", raw_reason="stop")])
    if exit_kind == "cancel":
        return _CancelBeforeTheLastEvent(world.sessions, "ses-b")
    return FakeExecutor([TextDelta(text="half an answer", index=0), RuntimeError("the executor blew up")])


async def _armed_turn_world() -> World:
    """A session whose turn is armed (a wake wrote its USER_INPUT at seq 4 and flipped it claimable) and is about to be claimed."""
    world = make_world()
    bus = InMemoryEventBus()
    await bus.initialize()
    world.bus = bus
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="claimable", turn_no=1, last_seq=4, created_at=T0)
    return world


def _dispatch_deps(world: World, executor: Any) -> SessionDispatchDeps:
    async def build(_row: Any) -> Any:
        return executor

    return SessionDispatchDeps(
        storage_provider=world.sp, workspace_io=world.registry.workspace("ws-1"), event_bus=world.bus, build_executor=build,
        workspace_registry=world.registry, scheduler=world.scheduler, claim_engine=world.engine,
    )


@pytest.mark.parametrize("exit_kind", ["clean", "cancel", "failure"])
async def test_a_steer_that_lands_the_moment_the_turn_reads_idle_takes_a_fresh_seq_after_the_turn(monkeypatch, exit_kind):
    """The audit's G7-03 (c) (scratch ``test_fc3``), in the production shape: a person's steer on another process lands the instant the ending
    turn's row first reads idle. On main that instant came before the turn wrote its last records and its ``last_seq``, so the USER_INPUT took a seq
    the turn had already written, and the turn's terminal record then landed after it. Now the row reads idle only once everything the turn
    wrote is on the row: the USER_INPUT takes the next seq, after every record of the turn, and arms the next turn."""
    async with asyncio.timeout(BOUND_S):
        world = await _armed_turn_world()
        try:
            _each_wake_in_its_own_process(monkeypatch)
            deliveries = []

            async def steer() -> None:
                deliveries.append(await deliver_steer(session_id="ses-b", text="a person's steer", parallelism="queue", human_intent=True,
                                                      **world.deliver_kwargs()))

            hook = _OnIdle(world.sessions, "ses-b", steer)
            world.sp._stores[WorkspaceSession] = hook

            await run_one_session_turn(_make_lease("ses-b"), _dispatch_deps(world, _exit_executor(world, exit_kind)))

            assert hook.fired and [d.outcome for d in deliveries] == ["woken"]
            seqs = _assert_seqs_unique(world)
            written = records(world, "ses-b")
            assert written[-1]["kind"] == "user_input" and written[-1]["seq"] == max(seqs), [(r["seq"], r["kind"]) for r in written]
            assert seqs == sorted(seqs), "the log is in seq order"
            row = await world.sessions.get("ses-b")
            assert (row.turn_status, row.last_seq) == ("claimable", max(seqs))
        finally:
            await world.bus.aclose()


async def test_a_steer_that_lands_the_moment_the_turn_reads_idle_goes_behind_a_message_queued_during_the_turn(monkeypatch):
    """G7-03 (b) through the real dispatch: a message queued during the turn is still waiting when a person's steer lands the instant the turn's
    row reads idle. On main the steer started the next turn and the drain then wrote the older message as a SECOND USER_INPUT for it, newest first.
    Now the steer queues behind it, and the drain starts the next turn with the older message alone."""
    async with asyncio.timeout(BOUND_S):
        world = await _armed_turn_world()
        try:
            _each_wake_in_its_own_process(monkeypatch)
            row = await world.sessions.get("ses-b")
            await store_pending_steer(storage_provider=world.sp, session=row, text="older, queued during the turn",
                                      workspace_registry=world.registry, event_bus=world.bus, attribution=PEER)
            deliveries = []

            async def steer() -> None:
                deliveries.append(await deliver_steer(session_id="ses-b", text="newer", parallelism="queue", human_intent=True,
                                                      **world.deliver_kwargs()))

            world.sp._stores[WorkspaceSession] = _OnIdle(world.sessions, "ses-b", steer)

            await run_one_session_turn(_make_lease("ses-b"), _dispatch_deps(world, _exit_executor(world, "clean")))

            assert [d.outcome for d in deliveries] == ["queued"]
            assert (await world.sessions.get("ses-b")).status == SessionStatus.RUNNING, "the drain woke the session that came to rest"
            _assert_seqs_unique(world)
            after_the_turn = [r for r in records(world, "ses-b") if r["seq"] > 4 and r["kind"] == "user_input"]
            assert [r["payload"]["text"] for r in after_the_turn] == ["older, queued during the turn"], "one USER_INPUT for the armed turn"
            assert await _pending_texts(world) == ["newer"]
            assert (await world.sessions.get("ses-b")).turn_status == "claimable"
        finally:
            await world.bus.aclose()
