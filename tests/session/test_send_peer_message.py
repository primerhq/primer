"""send_peer_message: one session hands a message to another (pivot P3, plan task 6; design 6.1).

The tests drive the REAL ``send_peer_message``, ``deliver_steer``, ``wake_session``, ``store_pending_steer`` and ``realize_next_pending`` over the
in-memory world of ``tests/session/_peer_fakes.py`` (the storage is the suite's fake provider, the event store and the dispatcher are the real ones);
nothing on the delivery path is stubbed except where a test names the race it makes. They walk the delivery table state by state, then the guards in
their order, and pin for every refusal that nothing was written: no ledger row, no queued message, no record, no event, no pulse.
"""

from __future__ import annotations

import asyncio
import gc
import json
from datetime import UTC, datetime, timedelta

import pytest

from primer.events.dispatcher import EventDispatcher
from primer.model.event import EventFilter, EventSubscription, FieldMatcher, SessionWakeSink
from primer.model.principal import PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    PeerMessage,
    PendingSessionMessage,
    SessionStatus,
)
from primer.session import peer_messages
from primer.session.peer_messages import (
    MAX_PEER_HOPS,
    NOTE_GRAPH,
    NOTE_GRAPH_STORED_INPUT,
    NOTE_QUEUED_BUSY,
    NOTE_QUEUED_EVENT,
    NOTE_QUEUED_GATE,
    NOTE_QUEUED_PAUSED,
    NOTE_REOPENED,
    NOTE_WOKEN,
    PEER_RATE_LIMIT,
    REFUSE_BUSY,
    REFUSE_ENDED,
    REFUSE_NO_CALLER,
    REFUSE_NOT_FOUND,
    REFUSE_RANK,
    REFUSE_SELF,
    PeerMessageRefused,
    PeerSendResult,
    send_peer_message,
)
from primer.session.pending_messages import realize_next_pending
from primer.session.steer_delivery import DELIVERED_SKIPPED_BUSY, SteerDelivery
from tests.session._peer_fakes import World, make_world, records, seed_session, slot

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
USER = PrincipalRef(type="user", id="u1", display="u1", role="user", source="local")
ADMIN = PrincipalRef(type="user", id="u2", display="u2", role="admin", source="local")
PREFIXED = "[from session ses-a (agent planner)]\nping"


async def _world(**sender_fields) -> World:
    """A world with the sender ``ses-a``: an agent session of workspace ``ws-1`` in the middle of a turn, started by a user."""
    world = make_world(with_bus=True)
    fields = {"status": SessionStatus.RUNNING, "turn_status": "running", "turn_no": 1, "initiated_by": USER, **sender_fields}
    await seed_session(world, sid="ses-a", binding=AgentSessionBinding(agent_id="planner"), **fields)
    return world


async def _send(world: World, target: str, text: str = "ping", *, sender: str = "ses-a", principal=USER, **kw) -> PeerSendResult:
    kw.setdefault("now", NOW)
    return await send_peer_message(
        sender_session_id=sender, sender_principal=principal, target_session_id=target, text=text, **kw, **world.deliver_kwargs(),
    )


async def _events(world: World, prefix: str):
    return await world.sp.get_event_store().read_after(0, event_type_prefix=prefix)


async def _pending(world: World) -> list[PendingSessionMessage]:
    return list((await world.sp.get_storage(PendingSessionMessage).find(None, OffsetPage(offset=0, length=200))).items)


async def _ledger(world: World) -> list[PeerMessage]:
    return list((await world.sp.get_storage(PeerMessage).find(None, OffsetPage(offset=0, length=200))).items)


def _kinds(world: World, sid: str, *, workspace_id: str = "ws-1") -> list[str]:
    return [r["kind"] for r in records(world, sid, workspace_id=workspace_id)]


async def _assert_nothing_stored(world: World, *, target: str = "ses-b", workspace_id: str = "ws-1") -> None:
    """A refusal writes nothing: no ledger row, no queued message, no record in either session, no event, no pulse on the scheduler or the claim."""
    assert await _ledger(world) == []
    assert await _pending(world) == []
    assert await _events(world, "session.") == []
    assert records(world, target, workspace_id=workspace_id) == [] and records(world, "ses-a") == []
    assert world.scheduler.enqueued == [] and world.engine.upserts == []


def _expect_refusal(info, error_type: str, message: str | None = None) -> None:
    assert info.value.error_type == error_type
    if message is not None:
        assert info.value.message == message


# ---- the delivery table: every state a target can be in ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param({"status": SessionStatus.WAITING, "turn_no": 1}, id="resting after a turn"),
        pytest.param({"status": SessionStatus.CREATED}, id="created, never ran"),
    ],
)
async def test_an_idle_session_starts_its_next_turn_with_the_message(seed):
    world = await _world()
    await seed_session(world, sid="ses-b", **seed)
    result = await _send(world, "ses-b")
    assert (result.delivery, result.hops, result.note, result.restarts_graph) == ("woken", 1, NOTE_WOKEN, False)
    assert (result.session_id, result.workspace_id, result.target_parked_on) == ("ses-b", "ws-1", None)
    assert slot(world, "ses-b").appended == [PREFIXED]
    user_input = [r for r in records(world, "ses-b") if r["kind"] == "user_input"][-1]
    assert user_input["payload"]["text"] == PREFIXED
    assert user_input["payload"]["peer"] == {
        "from_session_id": "ses-a", "from_workspace_id": "ws-1", "from_agent_id": "planner", "from_graph_id": None,
        "hops": 1, "message_id": result.message_id,
    }
    row = await world.sessions.get("ses-b")
    assert (row.status, row.turn_status, row.peer_hops) == (SessionStatus.RUNNING, "claimable", 1)
    assert world.scheduler.enqueued == ["ses-b"]
    assert [(m.id, m.delivery, m.hops) for m in await _ledger(world)] == [(result.message_id, "woken", 1)]
    [event] = await _events(world, "session.messaged")
    assert (event.session_id, event.workspace_id, event.correlation_id, event.actor) == ("ses-b", "ws-1", result.message_id, "user:u1")
    assert event.payload == {
        "message_id": result.message_id, "from_session_id": "ses-a", "from_workspace_id": "ws-1", "from_agent_id": "planner",
        "from_graph_id": None, "to_session_id": "ses-b", "to_workspace_id": "ws-1", "hops": 1, "mode": "auto", "delivery": "woken",
    }
    assert await _events(world, "session.steered") == []


async def test_a_running_session_gets_the_message_as_a_steer_at_its_next_turn_boundary():
    world = await _world()
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running", turn_no=1)
    result = await _send(world, "ses-b")
    assert (result.delivery, result.note, result.target_state) == ("queued", NOTE_QUEUED_BUSY, "running")
    [queued] = await _pending(world)
    assert queued.parts == [{"type": "text", "text": PREFIXED}]
    assert queued.attribution["peer"]["from_session_id"] == "ses-a" and queued.attribution["peer"]["hops"] == 1
    assert world.scheduler.enqueued == [] and world.engine.upserts == []
    assert "user_input" not in _kinds(world, "ses-b") and slot(world, "ses-b").appended == []
    assert (await world.sessions.get("ses-b")).peer_hops == 0, "a queued message has not started a turn yet"
    [messaged] = await _events(world, "session.messaged")
    [steered] = await _events(world, "session.steered")
    assert messaged.payload["delivery"] == "queued"
    assert (steered.session_id, steered.workspace_id, steered.correlation_id, steered.actor) == ("ses-b", "ws-1", result.message_id, "user:u1")
    assert steered.payload == {
        "has_instruction": True, "has_tool_results": False, "has_attachments": False, "from_session_id": "ses-a", "delivery": "queued",
    }
    assert [m.delivery for m in await _ledger(world)] == ["queued"]


async def test_an_armed_turn_counts_as_open_too():
    world = await _world()
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="claimable", turn_no=1)
    result = await _send(world, "ses-b")
    assert (result.delivery, result.note) == ("queued", NOTE_QUEUED_BUSY)
    assert len(await _events(world, "session.steered")) == 1


@pytest.mark.parametrize(
    ("key", "label"), [("ask_user:ses-b:call_q", "ask_user"), ("tool_approval:ses-b:call_t", "approval")],
)
async def test_a_message_to_a_parked_gate_is_queued_never_the_answer(key, label):
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, parked_status="parked", parked_event_key=key, parked_at=NOW)
    result = await _send(world, "ses-b")
    assert (result.delivery, result.target_parked_on, result.target_state) == ("queued", label, "parked")
    assert result.note == NOTE_QUEUED_GATE.format(parked_on=label)
    row = await world.sessions.get("ses-b")
    assert (row.parked_status, row.parked_event_key, row.turn_status) == ("parked", key, "idle"), "the park is untouched"
    assert "user_input" not in _kinds(world, "ses-b")
    assert not [k for k, _ in world.bus.published if k == key], "something answered the gate"
    assert len(await _pending(world)) == 1 and world.scheduler.enqueued == []
    assert len(await _events(world, "session.steered")) == 1


async def test_a_session_waiting_on_session_messaged_is_woken_and_the_message_stays_queued():
    """The doorbell: ``session.messaged`` carries the attribution, never the text; the message itself stays queued for the turn boundary."""
    world = await _world()
    key = "evwait:ses-b:call_w"
    await seed_session(world, sid="ses-b", turn_no=1, parked_status="parked", parked_event_key=key, parked_at=NOW)
    sub = EventSubscription(
        id="evsub-1",
        filter=EventFilter(event_types=["session.messaged"], fields=[FieldMatcher(path="session_id", op="eq", value="ses-b")]),
        sink=SessionWakeSink(event_key=key, session_id="ses-b"),
    )
    await world.sp.get_storage(EventSubscription).create(sub)
    store = world.sp.get_event_store()
    await store.set_cursor(sub.id, await store.max_id())

    result = await _send(world, "ses-b")
    assert (result.delivery, result.note, result.target_parked_on) == ("queued", NOTE_QUEUED_EVENT, "wait_for_event")
    assert await EventDispatcher(storage_provider=world.sp, event_bus=world.bus).drain_once() == 1

    woken = [payload for k, payload in world.bus.published if k == key]
    assert len(woken) == 1
    assert woken[0]["event_type"] == "session.messaged" and woken[0]["payload"]["from_session_id"] == "ses-a"
    assert "ping" not in json.dumps(woken[0]), "the text must not ride the event"
    [queued] = await _pending(world)
    assert queued.parts == [{"type": "text", "text": PREFIXED}], "the message arrives at the turn boundary, not through the event"
    assert "user_input" not in _kinds(world, "ses-b")


async def test_a_wait_for_another_session_is_not_woken_by_this_message():
    world = await _world()
    key = "evwait:ses-b:call_w"
    await seed_session(world, sid="ses-b", turn_no=1, parked_status="parked", parked_event_key=key, parked_at=NOW)
    sub = EventSubscription(
        id="evsub-1",
        filter=EventFilter(event_types=["session.messaged"], fields=[FieldMatcher(path="session_id", op="eq", value="ses-b")]),
        sink=SessionWakeSink(event_key=key, session_id="ses-b"),
    )
    await world.sp.get_storage(EventSubscription).create(sub)
    store = world.sp.get_event_store()
    await store.set_cursor(sub.id, await store.max_id())
    await seed_session(world, sid="ses-c", turn_no=1)

    await _send(world, "ses-c")
    assert await EventDispatcher(storage_provider=world.sp, event_bus=world.bus).drain_once() == 0
    assert not [k for k, _ in world.bus.published if k == key]


async def test_a_paused_session_stays_paused_and_queues_the_message():
    world = await _world()
    await seed_session(world, sid="ses-b", status=SessionStatus.PAUSED, turn_no=1)
    result = await _send(world, "ses-b")
    assert (result.delivery, result.note) == ("queued", NOTE_QUEUED_PAUSED)
    row = await world.sessions.get("ses-b")
    assert (row.status, row.pause_requested, row.turn_status) == (SessionStatus.PAUSED, False, "idle")
    [queued] = await _pending(world)
    assert queued.parts == [{"type": "text", "text": PREFIXED}]
    [superseded] = [r for r in records(world, "ses-b") if r["kind"] == "pause_superseded"]
    assert superseded["payload"]["action"] == "queued"
    assert world.scheduler.enqueued == [] and "user_input" not in _kinds(world, "ses-b")
    assert await _events(world, "session.steered") == [], "the target had no turn open: a paused session is held, not steered"
    assert [e.payload["delivery"] for e in await _events(world, "session.messaged")] == ["queued"]
    assert [m.delivery for m in await _ledger(world)] == ["queued"]


async def test_a_session_paused_while_running_queues_the_message_and_reads_as_paused():
    world = await _world()
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running", pause_requested=True, turn_no=1)
    result = await _send(world, "ses-b")
    assert (result.delivery, result.note) == ("queued", NOTE_QUEUED_PAUSED)
    assert (await world.sessions.get("ses-b")).pause_requested is True
    assert len(await _pending(world)) == 1


async def test_an_ended_session_reopens_with_the_message():
    world = await _world()
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_no=2, metadata={"invocation": 1})
    result = await _send(world, "ses-b")
    assert (result.delivery, result.note, result.target_state, result.target_parked_on) == ("woken", NOTE_REOPENED, "ended", None)
    row = await world.sessions.get("ses-b")
    assert (row.status, row.ended_reason, row.metadata["invocation"], row.peer_hops) == (SessionStatus.RUNNING, None, 2, 1)
    assert _kinds(world, "ses-b") == ["invocation_divider", "user_input"]
    assert slot(world, "ses-b").reopened is True and slot(world, "ses-b").appended == [PREFIXED]


async def test_an_ended_session_that_still_carries_its_park_is_reopened_not_queued_and_waits_for_nothing():
    """Cancel keeps the park columns on the row it ends; the session is ended, so there is no turn to queue behind and nothing it waits on."""
    world = await _world()
    await seed_session(
        world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="cancelled", turn_no=2, metadata={"invocation": 1},
        parked_status="parked", parked_event_key="ask_user:ses-b:call_q", parked_at=NOW,
    )
    result = await _send(world, "ses-b")
    assert (result.delivery, result.note, result.target_parked_on) == ("woken", NOTE_REOPENED, None)
    assert await _pending(world) == []
    assert len(await _events(world, "session.steered")) == 0


async def test_an_ended_session_whose_turn_status_still_reads_running_reopens_with_the_message():
    """Pinned choice (lead note on #761): an ENDED row can still carry ``turn_status="running"`` (the pool ends a session before the release clears the flag).
    The session is ended, so the message reopens it; it is not queued behind a turn that is over."""
    world = await _world()
    await seed_session(
        world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="cancelled", turn_status="running", turn_no=2, metadata={"invocation": 1},
    )
    result = await _send(world, "ses-b")
    assert (result.delivery, result.note, result.target_state) == ("woken", NOTE_REOPENED, "ended")
    assert (await world.sessions.get("ses-b")).turn_status == "claimable" and await _pending(world) == []
    assert await _events(world, "session.steered") == [], "no turn was open: the session had ended"


@pytest.mark.parametrize("ended_reason", ["force_deleted", "workspace_lost"])
async def test_an_ended_session_that_cannot_reopen_is_a_conflict(ended_reason):
    world = await _world()
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason=ended_reason, turn_no=2, peer_hops=2)
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-b")
    _expect_refusal(info, "conflict", f"session 'ses-b' has ended ({ended_reason}) and cannot take a message")
    assert (await world.sessions.get("ses-b")).peer_hops == 2
    await _assert_nothing_stored(world)


async def test_a_graph_session_gets_the_raw_text_and_the_result_says_it_restarts():
    world = await _world()
    await seed_session(world, sid="ses-g", binding=GraphSessionBinding(graph_id="triage"), turn_no=1)
    result = await _send(world, "ses-g")
    assert slot(world, "ses-g").appended == ["ping"], "the prefix would become part of the graph's input"
    user_input = [r for r in records(world, "ses-g") if r["kind"] == "user_input"][-1]
    assert user_input["payload"]["text"] == "ping" and user_input["payload"]["peer"]["from_session_id"] == "ses-a"
    assert (result.restarts_graph, result.delivery) == (True, "woken")
    assert result.note == NOTE_WOKEN + NOTE_GRAPH


async def test_a_graph_session_started_with_a_stored_input_says_the_message_is_only_recorded():
    world = await _world()
    await seed_session(
        world, sid="ses-g", binding=GraphSessionBinding(graph_id="triage"), turn_no=1, metadata={"graph_input": {"ticket": "INC-1"}},
    )
    result = await _send(world, "ses-g")
    assert result.note == NOTE_WOKEN + NOTE_GRAPH + NOTE_GRAPH_STORED_INPUT
    assert result.restarts_graph is True


async def test_a_queued_message_to_a_graph_session_keeps_the_raw_text_and_the_graph_note():
    world = await _world()
    await seed_session(world, sid="ses-g", binding=GraphSessionBinding(graph_id="triage"), status=SessionStatus.RUNNING, turn_status="running", turn_no=1)
    result = await _send(world, "ses-g")
    [queued] = await _pending(world)
    assert queued.parts == [{"type": "text", "text": "ping"}]
    assert result.note == NOTE_QUEUED_BUSY + NOTE_GRAPH


async def test_a_graph_session_can_send_and_is_named_by_its_graph():
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-g", binding=GraphSessionBinding(graph_id="triage"), initiated_by=USER, turn_no=1)
    await seed_session(world, sid="ses-b", turn_no=1)
    result = await _send(world, "ses-b", sender="ses-g")
    assert slot(world, "ses-b").appended == ["[from session ses-g (graph triage)]\nping"]
    user_input = [r for r in records(world, "ses-b") if r["kind"] == "user_input"][-1]
    assert (user_input["payload"]["peer"]["from_agent_id"], user_input["payload"]["peer"]["from_graph_id"]) == (None, "triage")
    [event] = await _events(world, "session.messaged")
    assert (event.payload["from_agent_id"], event.payload["from_graph_id"], event.correlation_id) == (None, "triage", result.message_id)


# ---- the guards, in their order ---------------------------------------------------------------------------------------------------


async def test_a_session_cannot_message_itself():
    world = await _world()
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-a")
    _expect_refusal(info, "bad-request", REFUSE_SELF)
    await _assert_nothing_stored(world)


async def test_a_message_from_something_that_is_not_a_session_is_refused():
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b")
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-b", sender="nobody")
    _expect_refusal(info, "unsupported-surface", REFUSE_NO_CALLER)
    await _assert_nothing_stored(world)


async def test_another_workspace_is_out_of_reach_unless_the_sender_is_in_the_default_workspace():
    world = await _world()
    await seed_session(world, sid="ses-x", workspace_id="ws-2", turn_no=1)
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-x")
    _expect_refusal(info, "not-found", "Session 'ses-x' does not exist in this workspace")
    with pytest.raises(PeerMessageRefused) as missing:
        await _send(world, "ses-nope")
    _expect_refusal(missing, "not-found", "Session 'ses-nope' does not exist in this workspace")
    assert info.value.message == REFUSE_NOT_FOUND.format(session_id="ses-x"), "out of reach reads exactly like a session that does not exist"
    await _assert_nothing_stored(world, target="ses-x", workspace_id="ws-2")

    await seed_session(world, sid="ses-p", workspace_id="primer", binding=AgentSessionBinding(agent_id="prime"), initiated_by=USER, turn_no=1)
    result = await _send(world, "ses-x", sender="ses-p")
    assert (result.delivery, result.workspace_id) == ("woken", "ws-2")
    user_input = [r for r in records(world, "ses-x", workspace_id="ws-2") if r["kind"] == "user_input"][-1]
    assert user_input["payload"]["peer"]["from_workspace_id"] == "primer"
    assert slot(world, "ses-x", workspace_id="ws-2").appended == ["[from session ses-p (agent prime)]\nping"]
    [event] = await _events(world, "session.messaged")
    assert (event.workspace_id, event.payload["from_workspace_id"], event.payload["to_workspace_id"]) == ("ws-2", "primer", "ws-2")


async def test_the_default_workspace_is_read_from_the_system_state_when_one_is_recorded(monkeypatch):
    """After pivot P2 the default workspace is whatever the deployment records, not the reserved id."""
    world = await _world()
    await seed_session(world, sid="ses-x", workspace_id="ws-2", turn_no=1)
    await seed_session(world, sid="ses-p", workspace_id="prime", binding=AgentSessionBinding(agent_id="prime"), initiated_by=USER, turn_no=1)
    await seed_session(world, sid="ses-q", workspace_id="primer", binding=AgentSessionBinding(agent_id="q"), initiated_by=USER, turn_no=1)

    async def _resolve(storage_provider):
        return "prime"

    monkeypatch.setattr(peer_messages, "resolve_default_workspace_id", _resolve)
    assert (await _send(world, "ses-x", sender="ses-p")).workspace_id == "ws-2"
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-x", sender="ses-q")
    _expect_refusal(info, "not-found")


async def test_a_lower_ranked_sender_is_forbidden():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, initiated_by=ADMIN)
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-b")
    _expect_refusal(info, "forbidden", REFUSE_RANK.format(session_id="ses-b", target_rank="admin", sender_rank="user"))
    assert "(admin)" in info.value.message and "(user)" in info.value.message
    await _assert_nothing_stored(world)


async def test_a_sender_of_equal_or_higher_rank_may_message():
    world = await _world(initiated_by=ADMIN)
    await seed_session(world, sid="ses-b", turn_no=1, initiated_by=USER)
    await seed_session(world, sid="ses-c", turn_no=1, initiated_by=ADMIN)
    assert (await _send(world, "ses-b", principal=ADMIN)).delivery == "woken"
    assert (await _send(world, "ses-c", principal=ADMIN)).delivery == "woken"


async def test_a_sender_with_no_principal_is_forbidden():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1)
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-b", principal=None)
    _expect_refusal(info, "forbidden")
    await _assert_nothing_stored(world)


async def test_out_of_reach_is_decided_before_rank_so_a_hidden_session_leaks_nothing():
    """A session of another workspace that also outranks the sender answers ``not-found``, not ``forbidden`` (which would confirm it exists)."""
    world = await _world()
    await seed_session(world, sid="ses-x", workspace_id="ws-2", turn_no=1, initiated_by=ADMIN)
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-x")
    _expect_refusal(info, "not-found")


async def test_a_reply_chain_stops_at_the_hop_limit():
    world = await _world(peer_hops=MAX_PEER_HOPS)
    await seed_session(world, sid="ses-b", turn_no=1)
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-b")
    assert info.value.error_type == "hop-limit"
    await _assert_nothing_stored(world)

    near = await _world(peer_hops=MAX_PEER_HOPS - 1)
    await seed_session(near, sid="ses-b", turn_no=1)
    result = await _send(near, "ses-b")
    assert result.hops == MAX_PEER_HOPS
    user_input = [r for r in records(near, "ses-b") if r["kind"] == "user_input"][-1]
    assert user_input["payload"]["peer"]["hops"] == MAX_PEER_HOPS


async def test_a_reply_chain_through_the_queue_is_refused_at_the_hop_limit():
    """Review focus 1: two agents answer each other, and each reply lands while the other is still in its turn, so it is QUEUED. If the queue dropped
    the hop count when it realized the message, every reply would restart at hop 1 and only the rate limit would stop the loop."""
    world = await _world()
    await seed_session(world, sid="ses-b", binding=AgentSessionBinding(agent_id="builder"), initiated_by=USER,
                       status=SessionStatus.RUNNING, turn_status="running", turn_no=1)

    async def start_turn(sid: str) -> None:
        row = await world.sessions.get(sid)
        row.turn_status = "running"
        await world.sessions.update(row)

    sender, target = "ses-a", "ses-b"
    for expected in range(1, MAX_PEER_HOPS + 1):
        result = await _send(world, target, sender=sender, text=f"hop {expected}")
        assert (result.hops, result.delivery) == (expected, "queued"), "the other side is mid-turn, so the reply is queued"
        # The target's turn ends; the drain realizes the queued message, which starts its next turn.
        assert await realize_next_pending(
            storage_provider=world.sp, workspace_id="ws-1", session_id=target, wake_deps=world.wake_deps(),
        ) is True
        assert (await world.sessions.get(target)).peer_hops == expected, "the hop count must survive the queue"
        await start_turn(target)
        sender, target = target, sender
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, target, sender=sender, text="one too many")
    assert info.value.error_type == "hop-limit"
    assert len(await _ledger(world)) == MAX_PEER_HOPS
    assert await _pending(world) == []


async def test_the_twenty_first_message_in_ten_minutes_is_refused_and_a_refusal_does_not_spend_the_allowance():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1)
    for _ in range(PEER_RATE_LIMIT - 1):
        await _send(world, "ses-b")
    for refused in (("ses-a", "bad-request"), ("ses-nope", "not-found")):
        with pytest.raises(PeerMessageRefused) as info:
            await _send(world, refused[0])
        assert info.value.error_type == refused[1]
    with pytest.raises(PeerMessageRefused) as busy:
        await _send(world, "ses-b", mode="if_idle")
    assert busy.value.error_type == "conflict"
    assert len(await _ledger(world)) == PEER_RATE_LIMIT - 1, "only a delivery is recorded"
    await _send(world, "ses-b")                                    # the twentieth still goes
    with pytest.raises(PeerMessageRefused) as limited:
        await _send(world, "ses-b")
    assert limited.value.error_type == "rate-limited"
    assert limited.value.message == "this session sent 20 messages in the last 10 minutes; retry in 600 seconds"
    assert len(await _ledger(world)) == PEER_RATE_LIMIT
    assert len(await _pending(world)) == PEER_RATE_LIMIT - 1, "the first woke the target, the rest queued; the refused one stored nothing"

    other = await seed_session(world, sid="ses-other", binding=AgentSessionBinding(agent_id="other"), initiated_by=USER, turn_no=1)
    assert (await _send(world, "ses-b", sender=other.id)).delivery == "queued", "another sender has its own allowance"
    later = NOW + timedelta(minutes=10, seconds=1)
    assert (await _send(world, "ses-b", now=later)).delivery == "queued", "ten minutes on, the allowance is back"


async def test_a_refusal_for_the_rate_names_when_to_retry():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1)
    for i in range(PEER_RATE_LIMIT):
        await _send(world, "ses-b", now=NOW - timedelta(seconds=30 * i))
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-b", now=NOW)
    assert info.value.message.endswith("retry in 30 seconds")


async def test_concurrent_sends_of_one_sender_cannot_both_pass_the_limit():
    """The check and the record are one critical section per sender. Without it every send reads the ledger before any of them has written."""
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1)
    target_slot = slot(world, "ses-b")

    async def append_instruction(content, *, extra_parts=None):
        await asyncio.sleep(0)                         # a real suspension inside the delivery, where the other sends get to run
        target_slot.appended.append(content)

    target_slot.append_instruction = append_instruction
    outcomes = await asyncio.gather(*(_send(world, "ses-b", text=f"ping {i}") for i in range(PEER_RATE_LIMIT + 5)), return_exceptions=True)
    delivered = [o for o in outcomes if isinstance(o, PeerSendResult)]
    refused = [o for o in outcomes if isinstance(o, PeerMessageRefused)]
    assert len(delivered) == PEER_RATE_LIMIT and len(refused) == 5, outcomes
    assert {r.error_type for r in refused} == {"rate-limited"}
    assert len(await _ledger(world)) == PEER_RATE_LIMIT
    assert len({o.message_id for o in delivered}) == PEER_RATE_LIMIT
    assert len(await _events(world, "session.messaged")) == PEER_RATE_LIMIT


async def test_the_rate_check_and_the_ledger_write_of_one_sender_never_overlap(monkeypatch):
    """The lock itself, observed directly: between one send's rate check and its ledger write no other send of the sender reads the ledger."""
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1)
    real_check, real_record = peer_messages.check_peer_rate, peer_messages.record_peer_message
    open_sections = 0
    peak = 0

    async def check(*args, **kwargs):
        nonlocal open_sections, peak
        open_sections += 1
        peak = max(peak, open_sections)
        try:
            await asyncio.sleep(0)                     # a send that is not excluded gets to run its own check here
            return await real_check(*args, **kwargs)
        except BaseException:
            open_sections -= 1                         # a refused send never reaches the ledger write
            raise

    async def record(*args, **kwargs):
        nonlocal open_sections
        try:
            return await real_record(*args, **kwargs)
        finally:
            open_sections -= 1

    monkeypatch.setattr(peer_messages, "check_peer_rate", check)
    monkeypatch.setattr(peer_messages, "record_peer_message", record)
    outcomes = await asyncio.gather(*(_send(world, "ses-b", text=f"ping {i}") for i in range(PEER_RATE_LIMIT + 3)), return_exceptions=True)
    assert sum(isinstance(o, PeerSendResult) for o in outcomes) == PEER_RATE_LIMIT
    assert peak == 1, "two sends of one sender were between their check and their write at once"
    assert open_sections == 0


async def test_two_senders_do_not_wait_for_each_other(monkeypatch):
    """The lock is per sender: a send that holds one sender's lock does not hold up another sender's."""
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1)
    await seed_session(world, sid="ses-c", turn_no=1)
    await seed_session(world, sid="ses-d", binding=AgentSessionBinding(agent_id="other"), initiated_by=USER, turn_no=1)
    release = asyncio.Event()
    entered = asyncio.Event()
    real_check = peer_messages.check_peer_rate

    async def check(storage_provider, sender_session_id, *, now):
        if sender_session_id == "ses-a":
            entered.set()
            await release.wait()                       # ses-a sits inside its critical section
        return await real_check(storage_provider, sender_session_id, now=now)

    monkeypatch.setattr(peer_messages, "check_peer_rate", check)
    first = asyncio.create_task(_send(world, "ses-b"))
    await entered.wait()
    other = await asyncio.wait_for(_send(world, "ses-c", sender="ses-d"), timeout=5)
    assert other.delivery == "woken" and not first.done()
    release.set()
    assert (await first).delivery == "woken"


async def test_the_locks_of_senders_that_are_done_are_not_kept():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1)
    for i in range(4):
        await seed_session(world, sid=f"ses-s{i}", binding=AgentSessionBinding(agent_id=f"s{i}"), initiated_by=USER, turn_no=1)
        await _send(world, "ses-b", sender=f"ses-s{i}")
    gc.collect()
    assert len(peer_messages._SENDER_LOCKS) == 0


# ---- mode: auto and if_idle -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seed", "reason"),
    [
        pytest.param({"status": SessionStatus.RUNNING, "turn_status": "running", "turn_no": 1}, "busy", id="running"),
        pytest.param({"status": SessionStatus.RUNNING, "turn_status": "claimable", "turn_no": 1}, "busy", id="armed"),
        pytest.param({"status": SessionStatus.PAUSED, "turn_no": 1}, "paused", id="paused while idle"),
        pytest.param({"status": SessionStatus.RUNNING, "turn_status": "running", "pause_requested": True, "turn_no": 1}, "paused", id="pause requested"),
        pytest.param(
            {"parked_status": "parked", "parked_event_key": "ask_user:ses-b:call_q", "parked_at": NOW, "turn_no": 1},
            "waiting on ask_user", id="parked on ask_user",
        ),
        pytest.param(
            {"parked_status": "parked", "parked_event_key": "evwait:ses-b:call_w", "parked_at": NOW, "turn_no": 1},
            "waiting on wait_for_event", id="parked on wait_for_event",
        ),
    ],
)
async def test_if_idle_refuses_a_busy_or_paused_session_and_stores_nothing(seed, reason):
    """Busy, paused, or waiting on something (parked): the session would not start a turn with the message now."""
    world = await _world()
    await seed_session(world, sid="ses-b", **seed)
    before = (await world.sessions.get("ses-b")).model_dump()
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-b", mode="if_idle")
    _expect_refusal(info, "conflict", REFUSE_BUSY.format(session_id="ses-b", reason=reason))
    assert (await world.sessions.get("ses-b")).model_dump() == before
    await _assert_nothing_stored(world)


async def test_if_idle_refuses_a_busy_session_in_the_words_of_the_plan():
    world = await _world()
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running", turn_no=1)
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-b", mode="if_idle")
    assert info.value.message == "session 'ses-b' is busy; nothing was sent (mode if_idle)"


async def test_if_idle_delivers_to_an_idle_session_and_the_event_says_so():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1)
    result = await _send(world, "ses-b", mode="if_idle")
    assert (result.delivery, result.note) == ("woken", NOTE_WOKEN)
    [event] = await _events(world, "session.messaged")
    assert event.payload["mode"] == "if_idle"


async def test_if_idle_reopens_an_ended_session_because_it_would_start_a_turn_now():
    world = await _world()
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_no=2, metadata={"invocation": 1})
    assert (await _send(world, "ses-b", mode="if_idle")).delivery == "woken"


async def test_an_if_idle_message_that_loses_the_race_to_a_new_turn_is_refused_and_stores_nothing(monkeypatch):
    """The target looked idle when the guard read it and had a turn armed by the time the delivery looked: the delivery skips it, nothing is recorded."""
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1)
    seen = {}

    async def deliver(**kwargs):
        seen.update(kwargs)
        return SteerDelivery(outcome=DELIVERED_SKIPPED_BUSY, session_id=kwargs["session_id"])

    monkeypatch.setattr(peer_messages, "deliver_steer", deliver)
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-b", mode="if_idle")
    _expect_refusal(info, "conflict", REFUSE_BUSY.format(session_id="ses-b", reason="busy"))
    assert (seen["parallelism"], seen["human_intent"], seen["session_id"]) == ("skip", False, "ses-b")
    await _assert_nothing_stored(world)


async def test_auto_delivers_by_the_targets_state_with_queue_parallelism(monkeypatch):
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1)
    seen = {}

    async def deliver(**kwargs):
        seen.update(kwargs)
        return SteerDelivery(outcome="woken", session_id=kwargs["session_id"])

    monkeypatch.setattr(peer_messages, "deliver_steer", deliver)
    await _send(world, "ses-b")
    assert (seen["parallelism"], seen["human_intent"]) == ("queue", False), "an agent's message is never a person's intent to resume a pause"
    assert seen["peer"].from_session_id == "ses-a" and seen["peer"].hops == 1 and seen["text"] == PREFIXED


# ---- a delivery that cannot happen ------------------------------------------------------------------------------------------------


async def test_a_workspace_removed_under_the_write_is_a_not_found_in_the_words_of_the_wake():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1)

    async def vanish(content, *, extra_parts=None):
        world.registry.remove("ws-1")
        raise FileNotFoundError(2, "No such file or directory")

    slot(world, "ses-b").append_instruction = vanish
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-b")
    _expect_refusal(info, "not-found", "workspace 'ws-1' was removed while writing session 'ses-b''s instruction")
    assert await _ledger(world) == [] and await _events(world, "session.") == []
    assert (await world.sessions.get("ses-b")).peer_hops == 0


async def test_a_session_deleted_between_the_read_and_the_delivery_is_not_found(monkeypatch):
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1)

    async def deliver(**kwargs):
        return SteerDelivery(outcome="missing")

    monkeypatch.setattr(peer_messages, "deliver_steer", deliver)
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-b")
    _expect_refusal(info, "not-found", REFUSE_NOT_FOUND.format(session_id="ses-b"))
    await _assert_nothing_stored(world)


# ---- the text ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "x" * 32_001], ids=["empty", "over the cap"])
async def test_a_text_the_tool_would_have_rejected_is_refused_here_too(text):
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1)
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-b", text=text)
    _expect_refusal(info, "bad-request")
    await _assert_nothing_stored(world)


async def test_a_mode_the_tool_would_have_rejected_is_refused_here_too():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1)
    with pytest.raises(PeerMessageRefused) as info:
        await _send(world, "ses-b", mode="always")
    _expect_refusal(info, "bad-request", "mode is auto or if_idle, not 'always'")
    await _assert_nothing_stored(world)


async def test_a_text_at_the_cap_is_delivered_whole():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1)
    text = "x" * 32_000
    await _send(world, "ses-b", text=text)
    assert slot(world, "ses-b").appended == [PREFIXED.replace("ping", text)]


# ---- the result -------------------------------------------------------------------------------------------------------------------


async def test_the_result_is_the_tools_answer():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1)
    result = await _send(world, "ses-b")
    body = result.model_dump(mode="json")
    assert set(body) == {
        "message_id", "session_id", "workspace_id", "delivery", "hops", "target_state", "target_parked_on", "restarts_graph", "note",
    }
    assert body["message_id"].startswith("pmsg-")
    assert body["target_state"] == "parked", "a session resting after a turn reads as parked in the served vocabulary; target_parked_on says it waits for no one"
    assert body["target_parked_on"] is None
    json.dumps(body)
