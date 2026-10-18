"""A peer message keeps its sender and its hop count through the wake, the queue and the realize (pivot P3, plan task 2).

Everything here drives the REAL ``deliver_steer``, ``wake_session``, ``store_pending_steer`` and ``realize_next_pending`` over the in-memory
world of ``tests/session/_peer_fakes.py``; nothing on the delivery path is stubbed. The matrix tests walk every state a target session can
be in (idle, ended, armed, running, parked on each gate, paused each way, graph-bound) and pin what is written for each.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from primer.int.claim import ClaimKind
from primer.model.storage import OffsetPage
from primer.model.workspace_session import (
    PEER_PAYLOAD_KEY,
    GraphSessionBinding,
    PeerAttribution,
    PendingSessionMessage,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.enqueue import wake_session
from primer.session.pending_messages import realize_next_pending, store_pending_steer
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


async def test_a_plain_message_to_a_busy_target_queues_with_no_attribution():
    world = make_world()
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running")
    out = await _send(world, "ses-b", text="from a trigger", peer=None)
    assert out.outcome == DELIVERED_QUEUED
    assert [p.attribution for p in await _pending(world)] == [None]


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


async def test_a_realized_message_for_a_session_that_was_paused_since_is_requeued_with_its_attribution():
    """The drain replays a queued message as a non-human wake, so a pause that landed meanwhile holds it again: the hop count must survive that."""
    world = make_world()
    row = await seed_session(world, sid="ses-b", status=SessionStatus.PAUSED, turn_no=1, peer_hops=1)
    queued = PEER.model_copy(update={"hops": 5})
    await store_pending_steer(
        storage_provider=world.sp, session=row, text=TEXT, workspace_registry=world.registry, attribution=queued.as_attribution(),
    )
    assert await realize_next_pending(
        storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", wake_deps=world.wake_deps(),
    ) is True
    after = await world.sessions.get("ses-b")
    assert (after.status, after.peer_hops) == (SessionStatus.PAUSED, 1)
    pending = await _pending(world)
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
