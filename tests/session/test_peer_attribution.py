"""A peer message keeps its sender and its hop count through the wake, the queue and the realize (pivot P3, plan task 2).

Everything here drives the REAL ``deliver_steer``, ``wake_session``, ``store_pending_steer`` and ``realize_next_pending`` over the in-memory
world of ``tests/session/_peer_fakes.py``; nothing on the delivery path is stubbed. The matrix tests walk every state a target session can
be in (idle, ended, armed, running, parked on each gate, paused each way, graph-bound) and pin what is written for each.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from primer.int.claim import ClaimKind
from primer.model.except_ import ConflictError
from primer.model.storage import OffsetPage
from primer.model.workspace_session import (
    MACHINE_ATTRIBUTION_KEY,
    PEER_PAYLOAD_KEY,
    GraphSessionBinding,
    PeerAttribution,
    PendingSessionMessage,
    SessionStatus,
    WorkspaceSession,
)
from primer.session import steer_delivery
from primer.session.enqueue import wake_session
from primer.session.pending_messages import _MAX_PENDING_PER_SESSION, realize_next_pending, store_pending_steer
from primer.session.steer_delivery import (
    DELIVERED_MISSING,
    DELIVERED_QUEUED,
    DELIVERED_SKIPPED_BUSY,
    DELIVERED_WOKEN,
    deliver_steer,
)
from tests.session._peer_fakes import make_world, records, seed_session, slot

PEER = PeerAttribution(
    from_session_id="ses-a", from_workspace_id="ws-1", from_agent_id="planner", hops=3, message_id="pmsg-1",
)
TEXT = "[from session ses-a (agent planner)]\nping"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


async def _pending(world) -> list[PendingSessionMessage]:
    return list((await world.sp.get_storage(PendingSessionMessage).find(None, OffsetPage(offset=0, length=50))).items)


def _user_inputs(world, sid: str) -> list[dict]:
    return [r for r in records(world, sid) if r["kind"] == "user_input"]


async def _send(world, sid: str, *, peer=PEER, text: str = TEXT, human_intent: bool = False, parallelism: str = "queue"):
    return await deliver_steer(
        session_id=sid, text=text, parallelism=parallelism, human_intent=human_intent, peer=peer, **world.deliver_kwargs(),
    )


# ---- the model --------------------------------------------------------------------------------------------------------------------


def test_the_attribution_round_trips_through_the_dict_the_queue_stores():
    stored = PEER.as_attribution()
    assert stored == {PEER_PAYLOAD_KEY: PEER.model_dump(mode="json")}
    assert PeerAttribution.from_attribution(stored) == PEER


@pytest.mark.parametrize("attribution", [None, {}, {"channel": "slack"}])
def test_an_attribution_without_a_peer_reads_as_none(attribution):
    assert PeerAttribution.from_attribution(attribution) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"peer": {"hops": "many"}},
        {"peer": {"from_session_id": "s", "from_workspace_id": "w", "hops": 0}},
        {"peer": {"from_session_id": "", "from_workspace_id": "w", "hops": 1}},
        {"peer": {"from_workspace_id": "w", "hops": 1}},
        {"peer": "ses-a"},
        {"peer": None},
    ],
)
def test_a_malformed_peer_value_is_a_validation_error(bad):
    with pytest.raises(ValidationError):
        PeerAttribution.from_attribution(bad)


def test_a_session_row_defaults_to_hop_zero_and_a_row_stored_without_the_key_reads_zero():
    world = make_world()
    row = WorkspaceSession.model_validate({
        "id": "ses-old", "workspace_id": "ws-1", "binding": {"kind": "agent", "agent_id": "a"},
        "status": "waiting", "created_at": NOW.isoformat(),
    })
    assert row.peer_hops == 0
    stored = world.sessions.seed_raw("ses-raw", {
        "workspace_id": "ws-1", "binding": {"kind": "agent", "agent_id": "a"}, "status": "waiting", "created_at": NOW.isoformat(),
    })
    assert stored.peer_hops == 0


# ---- the wake ---------------------------------------------------------------------------------------------------------------------


async def test_a_woken_peer_message_records_its_sender_and_sets_the_hop_count():
    world = make_world()
    await seed_session(world, sid="ses-b", turn_no=1)
    out = await _send(world, "ses-b")
    assert out.outcome == DELIVERED_WOKEN
    row = await world.sessions.get("ses-b")
    assert row.peer_hops == 3 and row.turn_status == "claimable"
    user_input = _user_inputs(world, "ses-b")[-1]
    assert user_input["payload"]["text"] == TEXT
    assert user_input["payload"]["peer"] == PEER.model_dump(mode="json")
    assert slot(world, "ses-b").appended == [TEXT]


async def test_a_wake_without_a_peer_starts_a_fresh_chain():
    world = make_world()
    await seed_session(world, sid="ses-b", turn_no=1, peer_hops=5)
    await _send(world, "ses-b", text="from a person", peer=None, human_intent=True)
    row = await world.sessions.get("ses-b")
    assert row.peer_hops == 0
    assert "peer" not in _user_inputs(world, "ses-b")[-1]["payload"]


async def test_a_wake_that_lands_no_message_leaves_the_hop_count_alone():
    """``peer_hops`` is the hop count of the message that started the turn: a wake that carries no message started no turn from one."""
    world = make_world()
    await seed_session(world, sid="ses-b", turn_no=1, peer_hops=5)
    await wake_session(
        workspace_id="ws-1", session_id="ses-b", instruction=None, human_intent=True, peer=PEER, deps=world.wake_deps(),
    )
    assert (await world.sessions.get("ses-b")).peer_hops == 5
    assert records(world, "ses-b") == []


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param({"status": SessionStatus.CREATED}, id="created, never ran"),
        pytest.param({"status": SessionStatus.WAITING, "turn_no": 1}, id="resting after a turn"),
        pytest.param(
            {"status": SessionStatus.WAITING, "turn_no": 1, "binding": GraphSessionBinding(graph_id="triage")}, id="graph-bound",
        ),
    ],
)
async def test_an_idle_target_starts_its_next_turn_with_the_message(seed):
    world = make_world()
    await seed_session(world, sid="ses-b", peer_hops=1, **seed)
    out = await _send(world, "ses-b")
    assert out.outcome == DELIVERED_WOKEN and out.session_id == "ses-b"
    row = await world.sessions.get("ses-b")
    assert (row.status, row.turn_status, row.peer_hops) == (SessionStatus.RUNNING, "claimable", 3)
    assert [r["payload"]["peer"] for r in _user_inputs(world, "ses-b")] == [PEER.model_dump(mode="json")]
    assert slot(world, "ses-b").appended == [TEXT]
    assert world.scheduler.enqueued == ["ses-b"] and world.engine.upserts == [(ClaimKind.SESSION, "ses-b")]
    assert await _pending(world) == []


async def test_an_ended_restartable_target_reopens_and_starts_a_turn_with_the_message():
    world = make_world()
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_no=2, metadata={"invocation": 1})
    out = await _send(world, "ses-b")
    assert out.outcome == DELIVERED_WOKEN
    row = await world.sessions.get("ses-b")
    assert (row.status, row.ended_reason, row.peer_hops, row.metadata["invocation"]) == (SessionStatus.RUNNING, None, 3, 2)
    assert [r["kind"] for r in records(world, "ses-b")] == ["invocation_divider", "user_input"]
    assert _user_inputs(world, "ses-b")[0]["payload"]["peer"]["hops"] == 3
    assert slot(world, "ses-b").reopened is True and slot(world, "ses-b").appended == [TEXT]


@pytest.mark.parametrize(
    ("ended_reason", "phase", "outcome"),
    [
        ("force_deleted", "running", DELIVERED_MISSING),
        ("workspace_lost", "ready", DELIVERED_MISSING),
        ("workspace_lost", "running", DELIVERED_WOKEN),
    ],
)
async def test_an_ended_target_that_cannot_reopen_takes_nothing(ended_reason, phase, outcome):
    """A target that cannot take the message reports ``missing`` and stores nothing: no record, no queued row, no pulse, no hop count."""
    world = make_world()
    world.registry.phase = phase
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason=ended_reason, turn_no=2, peer_hops=2)
    out = await _send(world, "ses-b")
    assert out.outcome == outcome
    row = await world.sessions.get("ses-b")
    if outcome == DELIVERED_MISSING:
        assert (row.status, row.ended_reason, row.peer_hops) == (SessionStatus.ENDED, ended_reason, 2)
        assert records(world, "ses-b") == [] and await _pending(world) == []
        assert world.scheduler.enqueued == [] and world.engine.upserts == []
    else:
        assert (row.status, row.peer_hops) == (SessionStatus.RUNNING, 3)


# ---- the queue --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param({"status": SessionStatus.RUNNING, "turn_status": "claimable"}, id="turn armed"),
        pytest.param({"status": SessionStatus.RUNNING, "turn_status": "running"}, id="turn running"),
        pytest.param(
            {"status": SessionStatus.RUNNING, "turn_status": "running", "pause_requested": True}, id="pause requested while running",
        ),
        pytest.param(
            {"parked_status": "parked", "parked_event_key": "ask_user:ses-b:call_q", "parked_at": NOW}, id="parked on ask_user",
        ),
        pytest.param(
            {"parked_status": "parked", "parked_event_key": "tool_approval:ses-b:call_t", "parked_at": NOW}, id="parked on an approval",
        ),
        pytest.param(
            {"parked_status": "parked", "parked_event_key": "evwait:ses-b:call_w", "parked_at": NOW}, id="parked on wait_for_event",
        ),
        pytest.param(
            {"parked_status": "parked", "parked_event_keys": ["evwait:ses-b:call_w", "timer:ses-b:call_t"], "parked_at": NOW},
            id="parked on several events",
        ),
        pytest.param(
            {"parked_status": "resumable", "parked_event_key": "ask_user:ses-b:call_q", "parked_at": NOW}, id="park fired, not yet resumed",
        ),
    ],
)
async def test_a_target_with_a_turn_open_queues_the_message_and_changes_nothing_else(seed):
    """Armed, running or parked (on any gate): the message waits behind the turn or the park. It is never written as a user message, never
    answers a gate, never pulses the scheduler, and does not touch the hop count (the queued message has not started a turn)."""
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", turn_no=1, peer_hops=2, **seed)
    before = (await world.sessions.get("ses-b")).model_dump(mode="json")
    out = await _send(world, "ses-b")
    assert out.outcome == DELIVERED_QUEUED and out.session_id == "ses-b"
    pending = await _pending(world)
    assert [p.attribution for p in pending] == [{"peer": PEER.model_dump(mode="json")}]
    assert [p.parts for p in pending] == [[{"type": "text", "text": TEXT}]]
    assert (await world.sessions.get("ses-b")).model_dump(mode="json") == before
    assert records(world, "ses-b") == [] and slot(world, "ses-b").appended == []
    assert world.scheduler.enqueued == [] and world.engine.upserts == []
    assert world.bus.published == [], "something was published on a key: a gate would have been answered"


async def test_a_busy_target_queues_the_message_with_its_attribution():
    world = make_world()
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running")
    out = await _send(world, "ses-b")
    assert out.outcome == DELIVERED_QUEUED
    assert [p.attribution for p in await _pending(world)] == [{"peer": PEER.model_dump(mode="json")}]
    assert (await world.sessions.get("ses-b")).peer_hops == 0  # the queued message has not started a turn


async def test_a_plain_message_to_a_busy_target_queues_with_no_peer_attribution():
    """Updated (ruling 1 of the G7 fixes): a message no person wrote (a trigger's, ``human_intent=False``) used to queue with no attribution at all;
    it now carries the machine mark, so the drain does not let it reopen a session cancelled before it is realized. A person's carries nothing."""
    world = make_world()
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running")
    out = await _send(world, "ses-b", text="from a trigger", peer=None)
    assert out.outcome == DELIVERED_QUEUED
    out = await _send(world, "ses-b", text="from a person", peer=None, human_intent=True)
    assert out.outcome == DELIVERED_QUEUED
    assert {p.parts[0]["text"]: p.attribution for p in await _pending(world)} == {
        "from a trigger": {MACHINE_ATTRIBUTION_KEY: True}, "from a person": None,
    }


async def test_skip_parallelism_drops_a_peer_message_for_a_busy_target_and_stores_nothing():
    world = make_world()
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running", peer_hops=2)
    out = await _send(world, "ses-b", parallelism="skip")
    assert out.outcome == DELIVERED_SKIPPED_BUSY
    assert await _pending(world) == [] and records(world, "ses-b") == []
    assert (await world.sessions.get("ses-b")).peer_hops == 2


# ---- the pause hold ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param({}, id="paused while idle (the pause route sets no flag)"),
        pytest.param({"pause_requested": True}, id="paused after the worker honoured the flag"),
    ],
)
async def test_a_pause_hold_reports_queued_and_keeps_the_attribution(seed):
    world = make_world()
    await seed_session(world, sid="ses-b", status=SessionStatus.PAUSED, turn_no=1, peer_hops=2, **seed)
    out = await _send(world, "ses-b")
    assert out.outcome == DELIVERED_QUEUED and out.session_id == "ses-b"
    row = await world.sessions.get("ses-b")
    assert (row.status, row.pause_requested, row.peer_hops) == (SessionStatus.PAUSED, bool(seed), 2)
    assert row.turn_status != "claimable"
    pending = await _pending(world)
    assert [p.attribution for p in pending] == [{"peer": PEER.model_dump(mode="json")}]
    superseded = [r for r in records(world, "ses-b") if r["kind"] == "pause_superseded"]
    assert [(r["payload"]["action"], r["payload"]["pending_id"]) for r in superseded] == [("queued", pending[0].id)]
    assert _user_inputs(world, "ses-b") == [] and slot(world, "ses-b").appended == []
    assert world.scheduler.enqueued == [] and world.engine.upserts == []


async def test_the_pause_route_pauses_an_idle_session_without_the_flag_and_a_machine_message_does_not_resume_it():
    """The premise of the hold, through the real route: pausing an idle session writes PAUSED and sets no ``pause_requested``, so a hold keyed on
    the flag alone let any trigger, agent or peer resume it."""
    from primer.api.routers.sessions import pause_session

    world = make_world()
    await seed_session(world, sid="ses-b", turn_no=1)
    await pause_session(workspace_id="ws-1", session_id="ses-b", sessions=world.sessions)
    paused = await world.sessions.get("ses-b")
    assert (paused.status, paused.pause_requested) == (SessionStatus.PAUSED, False)

    held = await _send(world, "ses-b", peer=None, text="from a trigger")
    assert held.outcome == DELIVERED_QUEUED
    assert (await world.sessions.get("ses-b")).status == SessionStatus.PAUSED
    assert world.scheduler.enqueued == [] and len(await _pending(world)) == 1

    resumed = await _send(world, "ses-b", peer=None, text="carry on", human_intent=True)
    assert resumed.outcome == DELIVERED_WOKEN
    row = await world.sessions.get("ses-b")
    assert (row.status, row.turn_status) == (SessionStatus.RUNNING, "claimable")
    assert world.scheduler.enqueued == ["ses-b"]


@pytest.mark.parametrize("seed", [{}, {"pause_requested": True}])
async def test_a_person_message_resumes_a_paused_session_and_starts_a_fresh_chain(seed):
    world = make_world()
    await seed_session(world, sid="ses-b", status=SessionStatus.PAUSED, turn_no=1, peer_hops=4, **seed)
    out = await _send(world, "ses-b", text="carry on", peer=None, human_intent=True)
    assert out.outcome == DELIVERED_WOKEN
    row = await world.sessions.get("ses-b")
    assert (row.status, row.pause_requested, row.turn_status, row.peer_hops) == (SessionStatus.RUNNING, False, "claimable", 0)
    assert await _pending(world) == []


# ---- the realize ------------------------------------------------------------------------------------------------------------------


async def test_a_realized_queued_peer_message_keeps_its_sender_and_hops():
    """Review Focus 1: a reply queued behind a turn must keep its hop count when the drain realizes it."""
    world = make_world()
    row = await seed_session(world, sid="ses-b", turn_no=1)
    queued = PEER.model_copy(update={"hops": 4})
    await store_pending_steer(
        storage_provider=world.sp, session=row, text=TEXT, workspace_registry=world.registry, attribution=queued.as_attribution(),
    )
    assert await realize_next_pending(
        storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", wake_deps=world.wake_deps(),
    ) is True
    assert (await world.sessions.get("ses-b")).peer_hops == 4
    user_input = _user_inputs(world, "ses-b")[-1]
    assert user_input["payload"]["peer"]["hops"] == 4
    assert user_input["payload"]["peer"]["from_session_id"] == "ses-a"
    assert user_input["payload"]["text"] == TEXT
    assert await _pending(world) == []


async def test_a_queued_message_is_realized_in_the_order_it_arrived_each_with_its_own_hops():
    world = make_world()
    row = await seed_session(world, sid="ses-b", turn_no=1)
    for text, hops in (("first", 2), ("second", 6)):
        peer = PEER.model_copy(update={"hops": hops})
        await store_pending_steer(
            storage_provider=world.sp, session=row, text=text, workspace_registry=world.registry, attribution=peer.as_attribution(),
        )
    seen = []
    for _ in range(2):
        assert await realize_next_pending(
            storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", wake_deps=world.wake_deps(),
        ) is True
        seen.append((await world.sessions.get("ses-b")).peer_hops)
        # The turn the message armed runs and ends (its exit flips the turn back to idle): the next boundary realizes the next message. While the
        # turn is armed a realize leaves it queued (one USER_INPUT per armed turn, audit G7-03).
        await world.sessions.patch_if("ses-b", {"turn_status": "idle"}, where={"turn_status": ["claimable"]})
    assert seen == [2, 6]
    assert [(r["payload"]["text"], r["payload"]["peer"]["hops"]) for r in _user_inputs(world, "ses-b")] == [("first", 2), ("second", 6)]


async def test_a_plain_queued_message_realizes_without_a_peer_and_resets_the_chain():
    world = make_world()
    row = await seed_session(world, sid="ses-b", turn_no=1, peer_hops=5)
    await store_pending_steer(storage_provider=world.sp, session=row, text="from a person", workspace_registry=world.registry)
    assert await realize_next_pending(
        storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", wake_deps=world.wake_deps(),
    ) is True
    assert (await world.sessions.get("ses-b")).peer_hops == 0
    assert "peer" not in _user_inputs(world, "ses-b")[-1]["payload"]


async def test_a_realized_message_for_a_session_that_was_paused_since_stays_queued_with_its_attribution():
    """The drain replays a queued message as a non-human wake, so a pause that landed meanwhile holds it: it stays where it is, the same row (it
    used to be stored again, at the back of the queue), and the hop count survives."""
    world = make_world()
    row = await seed_session(world, sid="ses-b", status=SessionStatus.PAUSED, turn_no=1, peer_hops=1)
    queued = PEER.model_copy(update={"hops": 5})
    stored = await store_pending_steer(
        storage_provider=world.sp, session=row, text=TEXT, workspace_registry=world.registry, attribution=queued.as_attribution(),
    )
    assert await realize_next_pending(
        storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", wake_deps=world.wake_deps(),
    ) is False
    after = await world.sessions.get("ses-b")
    assert (after.status, after.peer_hops) == (SessionStatus.PAUSED, 1)
    pending = await _pending(world)
    assert [p.id for p in pending] == [stored.id]
    assert [p.attribution for p in pending] == [{"peer": queued.model_dump(mode="json")}]
    assert [p.parts for p in pending] == [[{"type": "text", "text": TEXT}]]
    assert _user_inputs(world, "ses-b") == []


async def test_a_malformed_attribution_is_realized_as_a_plain_message(caplog):
    world = make_world()
    row = await seed_session(world, sid="ses-b", turn_no=1, peer_hops=2)
    await store_pending_steer(
        storage_provider=world.sp, session=row, text="x", workspace_registry=world.registry, attribution={"peer": {"hops": "many"}},
    )
    with caplog.at_level(logging.WARNING, logger="primer.session.pending_messages"):
        assert await realize_next_pending(
            storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", wake_deps=world.wake_deps(),
        ) is True
    assert (await world.sessions.get("ses-b")).peer_hops == 0
    assert "peer" not in _user_inputs(world, "ses-b")[-1]["payload"]
    assert "attribution" in caplog.text


# ---- an ended target that still carries its park -----------------------------------------------------------------------------------
# Two real writers end a session and leave its park columns set: Cancel of a paused or waiting row (an inline write that then deletes the lease, so no release
# ever clears them) and the workspace_lost reconcile (a patch that does not touch them). Park alone made route_steer say "a turn is open", so a steer to
# such a session was queued behind a park nobody will ever resolve: stored, never delivered, the session still ended. An ENDED session has no turn to
# queue behind: it is woken, which reopens it (clearing the park) or refuses it with a conflict that stores nothing.

_STALE_PARKS = {
    "parked": {"parked_status": "parked", "parked_event_key": "ask_user:ses-b:call_q", "parked_at": NOW},
    "resumable": {"parked_status": "resumable", "parked_event_key": "tool_approval:ses-b:call_t", "parked_at": NOW},
}


@pytest.mark.parametrize("park", sorted(_STALE_PARKS))
@pytest.mark.parametrize(
    ("ended_reason", "phase", "outcome"),
    [
        pytest.param("cancelled", "ready", DELIVERED_MISSING, id="cancelled: a peer message does not reopen it"),
        pytest.param("workspace_lost", "running", DELIVERED_WOKEN, id="workspace lost, the workspace is back"),
        pytest.param("workspace_lost", "ready", DELIVERED_MISSING, id="workspace lost, the workspace is down"),
    ],
)
async def test_a_target_that_ended_with_its_park_still_set_is_reopened_or_refused_never_queued(park, ended_reason, phase, outcome):
    world = make_world(with_bus=True)
    world.registry.phase = phase
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason=ended_reason, turn_no=2, peer_hops=2, **_STALE_PARKS[park])
    out = await _send(world, "ses-b")
    assert out.outcome == outcome
    row = await world.sessions.get("ses-b")
    if outcome == DELIVERED_WOKEN:
        assert (row.status, row.ended_reason, row.turn_status, row.peer_hops) == (SessionStatus.RUNNING, None, "claimable", 3)
        assert (row.parked_status, row.parked_event_key, row.parked_at) == (None, None, None), "the reopen did not clear the park"
        assert [r["payload"]["peer"]["from_session_id"] for r in _user_inputs(world, "ses-b")] == ["ses-a"]
        assert slot(world, "ses-b").reopened is True and slot(world, "ses-b").appended == [TEXT]
        assert world.scheduler.enqueued == ["ses-b"]
    else:
        assert (row.status, row.ended_reason, row.peer_hops) == (SessionStatus.ENDED, ended_reason, 2)
        assert row.parked_status == _STALE_PARKS[park]["parked_status"], "a refused steer must not touch the row"
        assert records(world, "ses-b") == [] and world.scheduler.enqueued == [] and world.engine.upserts == []
    assert await _pending(world) == [], "an ended session has no turn to queue behind, so nothing may be stored"
    assert not [key for key, _ in world.bus.published if key == _STALE_PARKS[park]["parked_event_key"]], "a gate was answered"


@pytest.mark.parametrize("peer", [PEER, None], ids=["a peer message", "a trigger or channel message"])
async def test_skip_parallelism_does_not_skip_an_ended_target_it_reopens_it(peer):
    """``skip`` drops a steer for a target with a turn in flight; an ended session has none. (It used to end the session as ``cancelled``; a peer message
    does not reopen one an operator cancelled, and the reason is not what this test is about.)"""
    world = make_world()
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_no=2, **_STALE_PARKS["resumable"])
    out = await _send(world, "ses-b", peer=peer, parallelism="skip")
    assert out.outcome == DELIVERED_WOKEN
    assert (await world.sessions.get("ses-b")).status == SessionStatus.RUNNING and await _pending(world) == []


async def _cancel_a_paused_parked_session(world) -> None:
    """The real Cancel of a paused session that is parked: it ends the row as ``cancelled`` and keeps the park (it deletes the lease, so no release clears it)."""
    from primer.workspace.session_factory import SessionCancelDeps, cancel_session

    class _Engine:
        async def delete_lease(self, kind, session_id):
            return None

    await seed_session(world, sid="ses-b", status=SessionStatus.PAUSED, pause_requested=True, turn_no=2, **_STALE_PARKS["resumable"])
    await cancel_session(
        workspace_id="ws-1", session_id="ses-b",
        deps=SessionCancelDeps(storage_provider=world.sp, scheduler=world.scheduler, claim_engine=_Engine(), event_bus=None),
    )
    ended = await world.sessions.get("ses-b")
    assert (ended.status, ended.ended_reason, ended.parked_status) == (SessionStatus.ENDED, "cancelled", "resumable"), "the premise: Cancel keeps the park"


async def test_the_real_cancel_of_a_paused_parked_session_leaves_it_ended_with_its_park_and_a_steer_reopens_it():
    """A person's steer: it used to be a peer message here, and a peer message does not reopen a session an operator cancelled (next test)."""
    world = make_world()
    await _cancel_a_paused_parked_session(world)

    out = await _send(world, "ses-b", peer=None, human_intent=True)
    assert out.outcome == DELIVERED_WOKEN
    row = await world.sessions.get("ses-b")
    assert (row.status, row.parked_status, row.turn_status) == (SessionStatus.RUNNING, None, "claimable")
    assert await _pending(world) == []


async def test_a_peer_message_does_not_reopen_the_session_the_real_cancel_ended():
    world = make_world()
    await _cancel_a_paused_parked_session(world)
    before = (await world.sessions.get("ses-b")).model_dump(mode="json")

    out = await _send(world, "ses-b")
    assert (out.outcome, out.conflict) == (DELIVERED_MISSING, True)
    assert (await world.sessions.get("ses-b")).model_dump(mode="json") == before, "the refused message changed the row"
    assert await _pending(world) == [] and records(world, "ses-b") == [] and slot(world, "ses-b").reopened is False


@pytest.mark.parametrize(("phase", "outcome"), [("running", DELIVERED_WOKEN), ("ready", DELIVERED_MISSING)])
async def test_the_real_workspace_lost_reconcile_of_a_parked_session_leaves_it_ended_with_its_park_and_a_steer_follows_the_workspace(phase, outcome):
    from primer.workspace.session_reconcile import reconcile_sessions_to_workspace_lost

    world = make_world()
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_no=2, **_STALE_PARKS["parked"])
    assert await reconcile_sessions_to_workspace_lost(world.sp, "ws-1") == 1
    ended = await world.sessions.get("ses-b")
    assert (ended.status, ended.ended_reason, ended.parked_status) == (SessionStatus.ENDED, "workspace_lost", "parked"), "the premise: the reconcile keeps the park"

    world.registry.phase = phase
    out = await _send(world, "ses-b")
    assert out.outcome == outcome
    assert await _pending(world) == []
    assert (await world.sessions.get("ses-b")).status == (SessionStatus.RUNNING if outcome == DELIVERED_WOKEN else SessionStatus.ENDED)


async def test_a_steer_in_the_window_where_end_session_has_written_ended_but_not_released_loses_no_message():
    """``WorkerPool._end_session`` writes ENDED while the row still carries its park (the release clears it later), then realizes ONE queued steer through
    ``wake_session``. A steer that lands in that window finds an ENDED row: it reopens the session itself and arms its turn. The steer ``_end_session``
    realizes right after finds that turn armed and stays queued, at the head, for the boundary of the turn the steer started (one USER_INPUT per armed
    turn, audit G7-03; it used to be written behind the steer as a second message for the same turn). Both reach the session, one turn each, the park
    is gone, and the claim is armed once for the pool to re-arm."""
    from primer.session.dispatch import realize_queued_steer

    world = make_world(with_bus=True)
    # The session ended ``completed`` (it used to be ``cancelled``): since ruling 1 of the G7 fixes no wake but a person's reopens a cancelled
    # session, and the window, not the reason, is what this test is about.
    row = await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_no=2, **_STALE_PARKS["parked"])
    queued_earlier = PEER.model_copy(update={"hops": 2})
    await store_pending_steer(
        storage_provider=world.sp, session=row, text="queued while it was parked", workspace_registry=world.registry,
        attribution=queued_earlier.as_attribution(),
    )

    out = await _send(world, "ses-b", peer=None, text="arrives in the window")
    assert out.outcome == DELIVERED_WOKEN
    assert (await world.sessions.get("ses-b")).parked_status is None

    await realize_queued_steer(
        storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", scheduler=world.scheduler,
        claim_engine=world.engine, workspace_registry=world.registry, event_bus=world.bus,
    )

    assert [r["payload"]["text"] for r in _user_inputs(world, "ses-b")] == ["arrives in the window"], "one USER_INPUT for the armed turn"
    assert [p.parts[0]["text"] for p in await _pending(world)] == ["queued while it was parked"]
    after = await world.sessions.get("ses-b")
    assert (after.status, after.turn_status, after.parked_status) == (SessionStatus.RUNNING, "claimable", None)

    # The turn the steer started ends (its exit flips it back to idle) and its boundary realizes the queued message, with its attribution.
    await world.sessions.patch_if("ses-b", {"turn_status": "idle"}, where={"turn_status": ["claimable"]})
    await realize_queued_steer(
        storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", scheduler=world.scheduler,
        claim_engine=world.engine, workspace_registry=world.registry, event_bus=world.bus,
    )
    assert await _pending(world) == []
    delivered = [(r["payload"]["text"], r["payload"].get("peer")) for r in _user_inputs(world, "ses-b")]
    assert [text for text, _ in delivered] == ["arrives in the window", "queued while it was parked"]
    assert delivered[1][1]["hops"] == 2, "the queued message lost its attribution"


@pytest.mark.parametrize("turn_status", ["running", "claimable"])
async def test_an_ended_target_whose_turn_status_still_reads_open_is_reopened_not_queued(turn_status):
    """Pinned choice (lead note on #761): the pool ends a session before the release clears its flags, so an ENDED row can still read ``running``. A steer
    reopens it (which resets the flag) instead of queueing behind a turn that is over: queued, it would never be delivered. The session ended as ``failed``
    (it used to be ``cancelled``): a peer message does not reopen a cancelled one, and is not queued behind its stale flag either
    (``test_a_peer_message_never_reopens_a_session_an_operator_cancelled`` in ``test_send_peer_message.py``)."""
    world = make_world()
    await seed_session(
        world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="failed", turn_no=2, turn_status=turn_status, metadata={"invocation": 1},
    )
    out = await _send(world, "ses-b")
    assert out.outcome == DELIVERED_WOKEN
    row = await world.sessions.get("ses-b")
    assert (row.status, row.ended_reason, row.turn_status, row.peer_hops) == (SessionStatus.RUNNING, None, "claimable", 3)
    assert [r["kind"] for r in records(world, "ses-b")] == ["invocation_divider", "user_input"]
    assert slot(world, "ses-b").reopened is True and await _pending(world) == []


async def test_a_binding_switch_queued_on_the_parked_session_waits_for_the_turn_that_answers_both_messages():
    """The ``_end_session`` window again, with a switch queued on the parked session (lead note on #761, documented here and in ``route_steer``).

    ``_end_session`` applies the queued switch with the guard that the row is still ENDED, then realizes one queued steer. A steer that lands between
    the ENDED write and that guard reopens the session itself, so the guard no longer holds: the switch is not applied and stays queued. The turn that
    answers the steer therefore runs under the OUTGOING binding; the queued message waits for that turn's boundary (one USER_INPUT per armed turn,
    audit G7-03), where the switch applies first and the message then runs under the INCOMING binding. Applying the switch before the reopen would
    need a variant of the applier that does not take the lifecycle lock the wake already holds.
    """
    from primer.session.dispatch import apply_queued_binding_switch, realize_queued_steer

    world = make_world(with_bus=True)
    request = {"kind": "agent", "agent_id": "builder", "profile_id": None, "actor": "user:u1"}
    # Ended ``completed`` (it used to be ``cancelled``): since ruling 1 of the G7 fixes no wake but a person's reopens a cancelled session, and the
    # window, not the reason, is what this test is about.
    row = await seed_session(
        world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_no=2, pending_binding_switch=request, **_STALE_PARKS["parked"],
    )
    await store_pending_steer(
        storage_provider=world.sp, session=row, text="queued while it was parked", workspace_registry=world.registry,
    )
    out = await _send(world, "ses-b", peer=None, text="arrives in the window")
    assert out.outcome == DELIVERED_WOKEN

    # What _end_session does next: apply the switch with its own guard, then realize ONE queued steer.
    io = world.registry.workspace("ws-1")
    await apply_queued_binding_switch(storage_provider=world.sp, workspace_io=io, session_id="ses-b", guard={"status": ["ended"]})
    await realize_queued_steer(
        storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", scheduler=world.scheduler,
        claim_engine=world.engine, workspace_registry=world.registry, event_bus=world.bus,
    )
    answering = await world.sessions.get("ses-b")
    assert answering.pending_binding_switch == request, "the guard (the row is ENDED) no longer held, so the switch stays queued"
    assert (answering.binding.agent_id, answering.binding_epoch) == ("agent-ses-b", 0), "the turn that answers the steer runs under the outgoing binding"
    assert [r["payload"]["text"] for r in _user_inputs(world, "ses-b")] == ["arrives in the window"]
    assert [p.parts[0]["text"] for p in await _pending(world)] == ["queued while it was parked"]

    # The checkpoint after that turn (its exit flipped it back to idle) applies the switch, then realizes the queued message: the turn ran, so the
    # session has no park, and the message runs under the incoming binding.
    await world.sessions.patch_if("ses-b", {"turn_status": "idle"}, where={"turn_status": ["claimable"]})
    await apply_queued_binding_switch(storage_provider=world.sp, workspace_io=io, session_id="ses-b")
    switched = await world.sessions.get("ses-b")
    assert switched.pending_binding_switch is None
    assert (switched.binding.agent_id, switched.binding_epoch) == ("builder", 1)
    await realize_queued_steer(
        storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", scheduler=world.scheduler,
        claim_engine=world.engine, workspace_registry=world.registry, event_bus=world.bus,
    )
    assert [r["payload"]["text"] for r in _user_inputs(world, "ses-b")] == ["arrives in the window", "queued while it was parked"]
    assert await _pending(world) == []


# ---- a full queue refuses a peer message, whichever way it would be queued (lead ruling R1 on #788) -----------------------------------


def _dropped(world) -> list[dict]:
    return [r["payload"] for r in records(world, "ses-b") if r["kind"] == "pause_superseded" and r["payload"]["action"] == "dropped"]


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param({"status": SessionStatus.RUNNING, "turn_status": "running"}, id="behind a turn: deliver_steer stores it"),
        pytest.param({"status": SessionStatus.PAUSED}, id="behind a pause: wake_session holds it"),
    ],
)
async def test_a_peer_message_to_a_full_queue_is_reported_queue_full_with_nothing_stored(seed):
    world = make_world(with_bus=True)
    row = await seed_session(world, sid="ses-b", turn_no=1, **seed)
    for i in range(_MAX_PENDING_PER_SESSION):
        await store_pending_steer(storage_provider=world.sp, session=row, text=f"steer {i}", workspace_registry=world.registry)
    queued = sorted(p.id for p in await _pending(world))
    before = (await world.sessions.get("ses-b")).model_dump(mode="json")

    out = await _send(world, "ses-b")

    assert (out.outcome, out.session_id) == (steer_delivery.DELIVERED_QUEUE_FULL, "ses-b")
    assert sorted(p.id for p in await _pending(world)) == queued
    assert (await world.sessions.get("ses-b")).model_dump(mode="json") == before
    assert records(world, "ses-b") == [] and world.scheduler.enqueued == [] and world.engine.upserts == []


async def test_a_message_that_is_not_a_peers_is_queued_at_a_full_queue_and_the_cap_drops_the_oldest_row_as_before():
    world = make_world(with_bus=True)
    row = await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running", turn_no=1)
    for i in range(_MAX_PENDING_PER_SESSION):
        await store_pending_steer(storage_provider=world.sp, session=row, text=f"steer {i}", workspace_registry=world.registry)

    out = await _send(world, "ses-b", peer=None, text="a trigger fired")

    assert out.outcome == DELIVERED_QUEUED
    assert len(await _pending(world)) == _MAX_PENDING_PER_SESSION
    assert "a trigger fired" in {p.parts[0]["text"] for p in await _pending(world)}
    assert len(_dropped(world)) == 1


# ---- a cancelled session stays cancelled for every wake no person stands behind (lead ruling R4 on #788, ruling 1 of the G7 fixes) ---------------
# Only a person's message or Restart reopens a session an operator cancelled. A peer message, a trigger fire, a tool call and a queued message
# realized after the Cancel do not (they wake with ``human_intent=False``). This used to hold for peer messages only: a trigger reopened it.


@pytest.mark.parametrize("park", sorted(_STALE_PARKS))
async def test_a_persons_message_reopens_a_cancelled_target_even_with_its_park_still_set(park):
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="cancelled", turn_no=2, peer_hops=2, **_STALE_PARKS[park])
    out = await _send(world, "ses-b", peer=None, human_intent=True)
    assert out.outcome == DELIVERED_WOKEN
    row = await world.sessions.get("ses-b")
    assert (row.status, row.ended_reason, row.turn_status, row.peer_hops) == (SessionStatus.RUNNING, None, "claimable", 0)
    assert (row.parked_status, row.parked_event_key) == (None, None)
    assert slot(world, "ses-b").reopened is True and await _pending(world) == []


@pytest.mark.parametrize("park", sorted(_STALE_PARKS))
async def test_a_trigger_or_tool_message_does_not_reopen_a_cancelled_target(park):
    """Re-pinned (ruling 1 of the G7 fixes): this was ``test_a_message_that_is_not_a_peers_reopens_...`` and pinned a trigger's message reopening the
    session the operator cancelled. A message no person stands behind is refused like a peer's, and nothing is written."""
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="cancelled", turn_no=2, peer_hops=2, **_STALE_PARKS[park])
    before = (await world.sessions.get("ses-b")).model_dump(mode="json")
    out = await _send(world, "ses-b", peer=None, human_intent=False)
    assert (out.outcome, out.conflict) == (DELIVERED_MISSING, True)
    assert "cancelled" in (out.detail or "")
    assert (await world.sessions.get("ses-b")).model_dump(mode="json") == before
    assert records(world, "ses-b") == [] and slot(world, "ses-b").reopened is False and await _pending(world) == []
    assert world.scheduler.enqueued == [] and world.engine.upserts == []


@pytest.mark.parametrize("peer", [PEER, None], ids=["a peer message", "a trigger or tool message"])
async def test_wake_session_refuses_a_wake_no_person_stands_behind_to_a_cancelled_session_before_it_writes_anything(peer):
    """The refusal sits in ``wake_session`` (``_reopen_ended_locked``), under the session's lifecycle lock and on the row read there, so a Cancel that
    lands after any earlier check is seen. It is a conflict, so ``deliver_steer`` reports the target as unable to take the message."""
    from primer.session.enqueue import OnlyAPersonReopens

    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="cancelled", turn_no=2, peer_hops=2, metadata={"invocation": 1})
    before = (await world.sessions.get("ses-b")).model_dump(mode="json")
    with pytest.raises(OnlyAPersonReopens) as info:
        await wake_session(
            workspace_id="ws-1", session_id="ses-b", instruction=TEXT, human_intent=False, peer=peer, deps=world.wake_deps(),
        )
    assert isinstance(info.value, ConflictError) and "cancelled" in str(info.value)
    assert (await world.sessions.get("ses-b")).model_dump(mode="json") == before
    assert records(world, "ses-b") == [] and slot(world, "ses-b").reopened is False and slot(world, "ses-b").appended == []
    assert world.scheduler.enqueued == [] and world.engine.upserts == [] and world.bus.published == []

    out = await _send(world, "ses-b", peer=peer)
    assert (out.outcome, out.conflict) == (DELIVERED_MISSING, True)


async def _queue(world, rows) -> None:
    """Queue ``rows`` on ``ses-b``, oldest first, as ``(text, peer)`` pairs, directly and at strictly increasing times in the past: which row is the oldest
    must not depend on the clock the test runs at."""
    base = datetime.now(UTC) - timedelta(hours=1)
    storage = world.sp.get_storage(PendingSessionMessage)
    for i, (text, peer) in enumerate(rows):
        ts = base + timedelta(seconds=i)
        await storage.create(PendingSessionMessage(
            id=f"ses-b:pending:{ts.isoformat()}:{i:08x}", session_id="ses-b", parts=[{"type": "text", "text": text}],
            attribution=peer.as_attribution() if peer is not None else None, enqueued_at=ts, created_at=ts,
        ))


async def _realize(world) -> None:
    """The drain checkpoint, as the cancelled exit of a turn and the pool's ``_end_session`` run it."""
    from primer.session.dispatch import realize_queued_steer

    await realize_queued_steer(
        storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", scheduler=world.scheduler,
        claim_engine=world.engine, workspace_registry=world.registry, event_bus=world.bus,
    )


async def test_a_queued_peer_message_is_not_realized_into_a_session_that_was_cancelled():
    """A peer message queued behind a running turn: the operator cancels, and the cancelled exit's checkpoint realizes the queue. That used to reopen the
    session the operator had just cancelled. The message is dropped on the record instead (the row is deleted before the wake, so it is not left behind)."""
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="cancelled", turn_no=2, peer_hops=2, metadata={"invocation": 1})
    await _queue(world, [(TEXT, PEER)])

    await _realize(world)

    after = await world.sessions.get("ses-b")
    assert (after.status, after.ended_reason, after.turn_status, after.peer_hops) == (SessionStatus.ENDED, "cancelled", "idle", 2)
    assert await _pending(world) == []
    assert [r["kind"] for r in records(world, "ses-b")] == ["pause_superseded"], "no divider, no user message: the session was not reopened"
    [dropped] = _dropped(world)
    assert dropped["text"] == TEXT and "cancelled" in dropped["reason"]
    assert slot(world, "ses-b").reopened is False and slot(world, "ses-b").appended == []
    assert world.scheduler.enqueued == [] and world.engine.upserts == []


async def test_the_cancelled_exits_own_checkpoint_does_not_reopen_the_session_for_a_queued_peer_message():
    """The checkpoint a cancelled turn runs last (``_land_cancelled_turn`` calls ``_realize_pending_at_checkpoint``, dispatch.py), through the dispatch
    deps, not the shared body alone."""
    from primer.session.dispatch import SessionDispatchDeps, _realize_pending_at_checkpoint

    world = make_world(with_bus=True)
    row = await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="cancelled", turn_no=2, metadata={"invocation": 1})
    await _queue(world, [(TEXT, PEER)])
    deps = SessionDispatchDeps(
        storage_provider=world.sp, workspace_io=world.registry.workspace("ws-1"), event_bus=world.bus, build_executor=lambda _row: None,
        scheduler=world.scheduler, claim_engine=world.engine, workspace_registry=world.registry,
    )

    await _realize_pending_at_checkpoint(deps, row)

    after = await world.sessions.get("ses-b")
    assert (after.status, after.ended_reason, after.turn_status) == (SessionStatus.ENDED, "cancelled", "idle")
    assert [r["kind"] for r in records(world, "ses-b")] == ["pause_superseded"] and await _pending(world) == []
    assert world.scheduler.enqueued == [] and world.engine.upserts == []


async def test_a_persons_queued_steer_behind_a_dropped_peer_message_is_still_realized():
    """Exactly one row is realized per checkpoint, so a dropped peer message must not use the checkpoint up: the person's steer behind it reopens the
    session now. What queued behind the person's steer waits for the checkpoint after its turn, when the session is no longer cancelled."""
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="cancelled", turn_no=2, metadata={"invocation": 1})
    await _queue(world, [("peer 1", PEER), ("from a person", None), ("peer 2", PEER)])

    await _realize(world)

    after = await world.sessions.get("ses-b")
    assert (after.status, after.ended_reason, after.turn_status) == (SessionStatus.RUNNING, None, "claimable")
    assert [(r["payload"]["text"], r["payload"].get("peer")) for r in _user_inputs(world, "ses-b")] == [("from a person", None)]
    assert [d["text"] for d in _dropped(world)] == ["peer 1"]
    assert [p.parts[0]["text"] for p in await _pending(world)] == ["peer 2"]
    assert world.scheduler.enqueued == ["ses-b"]


async def test_a_persons_queued_steer_still_reopens_a_cancelled_session():
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="cancelled", turn_no=2, metadata={"invocation": 1})
    await _queue(world, [("from a person", None)])

    await _realize(world)

    after = await world.sessions.get("ses-b")
    assert (after.status, after.ended_reason, after.turn_status) == (SessionStatus.RUNNING, None, "claimable")
    assert [r["kind"] for r in records(world, "ses-b")] == ["invocation_divider", "user_input"] and _dropped(world) == []
    assert await _pending(world) == []


async def test_a_persons_restart_still_reopens_a_cancelled_session():
    """The other way a person reopens it: Restart resets the row and then wakes it with no peer."""
    from primer.session.reset import SessionResetDeps, restart_session

    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="cancelled", turn_no=2, metadata={"invocation": 1})

    await restart_session(
        workspace_id="ws-1", session_id="ses-b", instruction="carry on", human_intent=True,
        reset_deps=SessionResetDeps(storage_provider=world.sp, workspace_registry=world.registry, event_bus=world.bus), wake_deps=world.wake_deps(),
    )

    after = await world.sessions.get("ses-b")
    assert (after.status, after.ended_reason, after.turn_status) == (SessionStatus.RUNNING, None, "claimable")
    assert [r["payload"]["text"] for r in _user_inputs(world, "ses-b")] == ["carry on"]


@pytest.mark.parametrize("ended_reason", ["completed", "failed", "tool_turn_cap"])
async def test_a_queued_peer_message_is_still_realized_into_a_session_that_ended_for_another_reason(ended_reason):
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason=ended_reason, turn_no=2, metadata={"invocation": 1})
    await _queue(world, [(TEXT, PEER)])

    await _realize(world)

    after = await world.sessions.get("ses-b")
    assert (after.status, after.peer_hops) == (SessionStatus.RUNNING, 3)
    assert [r["payload"]["peer"]["from_session_id"] for r in _user_inputs(world, "ses-b")] == ["ses-a"] and _dropped(world) == []


# ---- a delivery that finds the session unable to take the message says why ---------------------------------------------------------


async def test_a_session_that_does_not_exist_is_missing_with_no_reason():
    out = await _send(make_world(), "nope")
    assert (out.outcome, out.session_id, out.detail, out.conflict) == (DELIVERED_MISSING, None, None, False)


async def test_a_session_that_cannot_be_reopened_is_missing_as_a_conflict_with_wake_sessions_words():
    world = make_world()
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="force_deleted", turn_no=2)
    out = await _send(world, "ses-b")
    assert (out.outcome, out.conflict) == (DELIVERED_MISSING, True)
    assert out.detail == "Session 'ses-b' ended as 'force_deleted' and cannot be re-opened."


async def test_a_workspace_removed_under_the_write_is_missing_as_a_not_found_with_wake_sessions_words():
    world = make_world()
    await seed_session(world, sid="ses-b", turn_no=1)

    async def vanish(content, *, extra_parts=None):
        world.registry.remove("ws-1")
        raise FileNotFoundError(2, "No such file or directory")

    slot(world, "ses-b").append_instruction = vanish
    out = await _send(world, "ses-b")
    assert (out.outcome, out.conflict) == (DELIVERED_MISSING, False)
    assert out.detail == "workspace 'ws-1' was removed while writing session 'ses-b''s instruction"
    assert await _pending(world) == [] and (await world.sessions.get("ses-b")).turn_status == "idle"
