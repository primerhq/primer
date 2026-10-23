"""steer_workspace_session called from inside a workspace session is a message from that session, and gets the peer guards (pivot P3, lead ruling 3).

The REST steer route and a call with no calling session (the MCP endpoint, a chat) are a person or an API client steering: they are unchanged
(``tests/toolset/test_steer_tool_queue_when_busy.py`` pins them). An AGENT in a session that calls the tool is another session putting a message into
the target, so it goes through ``send_peer_message``, the service ``message_session`` uses: the target sees who sent it (a prefix and ``payload.peer``), the
hop count and the per-sender rate limit apply, the target's creator may not outrank the sender, and the two lead rulings that are keyed on a peer message
(a peer never evicts a person's queued steer; a peer never reopens a session an operator cancelled) hold for it too. Since lead ruling 3 of the G7 fixes
the reach of the caller's workspace is one of the guards too (it used to be skipped): a session outside the default workspace reaches only its own
workspace, and a session out of reach answers exactly as one that does not exist. Its answer keeps the session row's keys and adds ``delivery``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from primer.model.principal import PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.workspace_session import AgentSessionBinding, GraphSessionBinding, PeerMessage, PendingSessionMessage, SessionStatus
from primer.model.yield_ import ToolContext
from primer.session.peer_messages import PEER_MESSAGE_MAX_CHARS, PEER_RATE_LIMIT, REFUSE_QUEUE_FULL
from primer.session.pending_messages import _MAX_PENDING_PER_SESSION, store_pending_steer
from primer.toolset.workspaces import build_workspaces_toolset
from tests.session._peer_fakes import make_world, records, seed_session, slot

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
INSTRUCTION = "Focus on the failing test first."
PREFIXED = f"[from session ses-a (agent planner)]\n{INSTRUCTION}"
USER = PrincipalRef(type="user", id="u1", display="u1", role="user", source="local")
ADMIN = PrincipalRef(type="user", id="u2", display="u2", role="admin", source="local")


def _ctx(session_id: str | None = "ses-a", workspace_id: str | None = "ws-1", initiated_by: PrincipalRef | None = USER) -> ToolContext:
    return ToolContext(tool_call_id="call_1", session_id=session_id, workspace_id=workspace_id, initiated_by=initiated_by)


async def _world():
    """A world with the calling session ``ses-a``: an agent session of ``ws-1`` that rests, started by a user."""
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-a", binding=AgentSessionBinding(agent_id="planner"), initiated_by=USER, created_at=T0)
    return world


async def _steer(world, session_id: str, *, workspace_id: str = "ws-1", instruction: str = INSTRUCTION, ctx: ToolContext | None = None):
    toolset = build_workspaces_toolset(
        storage_provider=world.sp, workspace_registry=world.registry, scheduler=world.scheduler, claim_engine=world.engine, event_bus=world.bus,
    )
    result = await toolset.call(
        tool_name="steer_workspace_session",
        arguments={"workspace_id": workspace_id, "session_id": session_id, "instruction": instruction},
        ctx=ctx,
    )
    return result, json.loads(result.output)


async def _events(world, prefix: str):
    return await world.sp.get_event_store().read_after(0, event_type_prefix=prefix)


async def _pending(world) -> list[PendingSessionMessage]:
    return list((await world.sp.get_storage(PendingSessionMessage).find(None, OffsetPage(offset=0, length=200))).items)


async def _ledger(world) -> list[PeerMessage]:
    return list((await world.sp.get_storage(PeerMessage).find(None, OffsetPage(offset=0, length=200))).items)


def _kinds(world, session_id: str, *, workspace_id: str = "ws-1") -> list[str]:
    return [r["kind"] for r in records(world, session_id, workspace_id=workspace_id)]


async def _assert_nothing_stored(world, *, target: str = "ses-b", workspace_id: str = "ws-1", queued: int = 0) -> None:
    assert await _ledger(world) == []
    assert len(await _pending(world)) == queued
    assert await _events(world, "session.") == []
    assert records(world, target, workspace_id=workspace_id) == [] and world.scheduler.enqueued == [] and world.engine.upserts == []


async def _fill_queue(world, target: str = "ses-b") -> None:
    row = await world.sessions.get(target)
    for i in range(_MAX_PENDING_PER_SESSION):
        await store_pending_steer(
            storage_provider=world.sp, session=row, text=f"a person's steer {i}", workspace_registry=world.registry, event_bus=world.bus,
        )


# ---- the message is attributed ----------------------------------------------------------------------------------------------------


async def test_a_steer_from_inside_a_session_is_a_message_from_that_session():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, created_at=T0 + timedelta(minutes=5))
    result, body = await _steer(world, "ses-b", ctx=_ctx())
    assert not result.is_error, result.output
    assert (body["status"], body["turn_status"], body["delivery"]) == ("running", "claimable", "woken")
    assert slot(world, "ses-b").appended == [PREFIXED]
    (user_input,) = [r for r in records(world, "ses-b") if r["kind"] == "user_input"]
    assert user_input["payload"]["text"] == PREFIXED
    assert user_input["payload"]["peer"] == {
        "from_session_id": "ses-a", "from_workspace_id": "ws-1", "from_agent_id": "planner", "from_graph_id": None, "hops": 1,
        "message_id": (await _ledger(world))[0].id,
    }
    assert (await world.sessions.get("ses-b")).peer_hops == 1
    assert [(m.from_session_id, m.to_session_id, m.hops, m.delivery) for m in await _ledger(world)] == [("ses-a", "ses-b", 1, "woken")]
    assert world.scheduler.enqueued == ["ses-b"]


async def test_a_steer_from_inside_a_session_is_announced_as_a_message_and_as_a_steer_whatever_the_target_does_with_it():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, created_at=T0 + timedelta(minutes=5))
    await seed_session(world, sid="ses-c", status=SessionStatus.RUNNING, turn_status="running", turn_no=1, created_at=T0 + timedelta(minutes=6))
    for target, delivery in (("ses-b", "woken"), ("ses-c", "queued")):
        result, body = await _steer(world, target, ctx=_ctx())
        assert not result.is_error and body["delivery"] == delivery, result.output
    messaged = await _events(world, "session.messaged")
    steered = await _events(world, "session.steered")
    assert [(e.session_id, e.actor, e.payload["delivery"], e.payload["mode"]) for e in messaged] == [
        ("ses-b", "user:u1", "woken", "auto"), ("ses-c", "user:u1", "queued", "auto"),
    ]
    assert [(e.session_id, e.actor, e.payload["delivery"], e.payload["from_session_id"]) for e in steered] == [
        ("ses-b", "user:u1", "woken", "ses-a"), ("ses-c", "user:u1", "queued", "ses-a"),
    ]
    assert all(e.payload["has_instruction"] is True and e.payload["has_tool_results"] is False for e in steered)
    assert all(INSTRUCTION not in json.dumps(e.payload) for e in messaged + steered)


async def test_a_busy_target_queues_the_steer_with_its_attribution_and_keeps_its_own_hop_count():
    world = await _world()
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running", turn_no=1, peer_hops=2, created_at=T0 + timedelta(minutes=5))
    result, body = await _steer(world, "ses-b", ctx=_ctx())
    assert not result.is_error and body["delivery"] == "queued", result.output
    (pending,) = await _pending(world)
    assert pending.parts == [{"type": "text", "text": PREFIXED}]
    assert pending.attribution["peer"]["from_session_id"] == "ses-a" and pending.attribution["peer"]["hops"] == 1
    assert (await world.sessions.get("ses-b")).peer_hops == 2, "a queued message has not started a turn"
    assert "user_input" not in _kinds(world, "ses-b") and world.scheduler.enqueued == []


async def test_a_graph_target_gets_the_instruction_as_it_is_with_the_sender_on_the_record():
    world = await _world()
    await seed_session(world, sid="ses-g", binding=GraphSessionBinding(graph_id="triage"), turn_no=1, created_at=T0 + timedelta(minutes=5))
    result, _ = await _steer(world, "ses-g", ctx=_ctx())
    assert not result.is_error, result.output
    assert slot(world, "ses-g").appended == [INSTRUCTION]
    (user_input,) = [r for r in records(world, "ses-g") if r["kind"] == "user_input"]
    assert user_input["payload"]["text"] == INSTRUCTION and user_input["payload"]["peer"]["from_session_id"] == "ses-a"


async def test_a_graph_session_that_steers_is_named_as_a_graph():
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-g", binding=GraphSessionBinding(graph_id="triage"), initiated_by=USER, created_at=T0)
    await seed_session(world, sid="ses-b", turn_no=1, created_at=T0 + timedelta(minutes=5))
    result, _ = await _steer(world, "ses-b", ctx=_ctx("ses-g"))
    assert not result.is_error, result.output
    assert slot(world, "ses-b").appended == [f"[from session ses-g (graph triage)]\n{INSTRUCTION}"]


async def test_the_answer_keeps_every_key_of_the_session_row_and_adds_delivery():
    world = await _world()
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running", turn_no=1, created_at=T0 + timedelta(minutes=5))
    row_keys = set((await world.sessions.get("ses-b")).model_dump(mode="json"))
    _, body = await _steer(world, "ses-b", ctx=_ctx())
    assert set(body) == row_keys | {"delivery"}
    assert body["id"] == "ses-b" and body["workspace_id"] == "ws-1"


# ---- the guards --------------------------------------------------------------------------------------------------------------------


async def test_a_steer_carries_the_senders_hop_count_and_stops_at_the_limit():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, created_at=T0 + timedelta(minutes=5))
    await seed_session(world, sid="ses-h3", binding=AgentSessionBinding(agent_id="planner"), initiated_by=USER, peer_hops=3, created_at=T0)
    await seed_session(world, sid="ses-h8", binding=AgentSessionBinding(agent_id="planner"), initiated_by=USER, peer_hops=8, created_at=T0)
    result, body = await _steer(world, "ses-b", ctx=_ctx("ses-h8"))
    assert result.is_error and body["type"] == "hop-limit"
    assert "8 hops" in body["message"] and "limit is 8" in body["message"]
    await _assert_nothing_stored(world)
    result, body = await _steer(world, "ses-b", ctx=_ctx("ses-h3"))
    assert not result.is_error, result.output
    (user_input,) = [r for r in records(world, "ses-b") if r["kind"] == "user_input"]
    assert user_input["payload"]["peer"]["hops"] == 4 and (await world.sessions.get("ses-b")).peer_hops == 4


async def test_a_steer_and_a_message_share_the_senders_allowance():
    """Both are a peer message: 20 in ten minutes between them, and the 21st is refused whichever tool sends it."""
    world = await _world()
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running", turn_no=1, created_at=T0 + timedelta(minutes=5))
    for i in range(PEER_RATE_LIMIT - 1):
        await world.sp.get_storage(PeerMessage).create(PeerMessage(
            from_session_id="ses-a", from_workspace_id="ws-1", to_session_id="ses-b", to_workspace_id="ws-1", hops=1, delivery="queued",
            sent_at=datetime.now(UTC) - timedelta(seconds=i),
        ))
    result, body = await _steer(world, "ses-b", ctx=_ctx())
    assert not result.is_error and body["delivery"] == "queued", result.output
    assert len(await _ledger(world)) == PEER_RATE_LIMIT
    before = len(await _pending(world))
    result, body = await _steer(world, "ses-b", ctx=_ctx())
    assert result.is_error and body["type"] == "rate-limited" and "sent 20 messages in the last 10 minutes" in body["message"]
    assert len(await _pending(world)) == before and len(await _ledger(world)) == PEER_RATE_LIMIT
    # another session of the same workspace has its own allowance
    await seed_session(world, sid="ses-d", binding=AgentSessionBinding(agent_id="coder"), initiated_by=USER, created_at=T0)
    result, _ = await _steer(world, "ses-b", ctx=_ctx("ses-d"))
    assert not result.is_error, result.output


async def test_a_steer_into_a_session_an_admin_started_is_forbidden_to_a_user_run():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, initiated_by=ADMIN, created_at=T0 + timedelta(minutes=5))
    result, body = await _steer(world, "ses-b", ctx=_ctx())
    assert result.is_error and body["type"] == "forbidden"
    assert "(admin)" in body["message"] and "(user)" in body["message"]
    await _assert_nothing_stored(world)
    result, body = await _steer(world, "ses-b", ctx=_ctx(initiated_by=ADMIN))
    assert not result.is_error and body["delivery"] == "woken", result.output


async def test_a_run_with_no_recorded_initiator_steers_as_an_ordinary_user():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, created_at=T0 + timedelta(minutes=5))
    await seed_session(world, sid="ses-c", turn_no=1, initiated_by=ADMIN, created_at=T0 + timedelta(minutes=6))
    result, body = await _steer(world, "ses-b", ctx=_ctx(initiated_by=None))
    assert not result.is_error and body["delivery"] == "woken", result.output
    result, body = await _steer(world, "ses-c", ctx=_ctx(initiated_by=None))
    assert result.is_error and body["type"] == "forbidden"


async def test_a_session_cannot_steer_itself():
    world = await _world()
    result, body = await _steer(world, "ses-a", ctx=_ctx())
    assert result.is_error and body == {"type": "bad-request", "message": "a session cannot message itself"}
    assert await _ledger(world) == [] and await _events(world, "session.") == [] and world.scheduler.enqueued == []


async def test_a_steer_from_a_session_that_is_gone_is_refused_not_sent_unattributed():
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", turn_no=1)
    result, body = await _steer(world, "ses-b", ctx=_ctx("ses-ghost"))
    assert result.is_error and body["type"] == "unsupported-surface"
    assert records(world, "ses-b") == [] and world.scheduler.enqueued == [] and await _ledger(world) == []


async def test_the_text_cap_of_a_message_applies_to_a_steer_from_a_session_and_to_no_one_else():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, created_at=T0 + timedelta(minutes=5))
    long_text = "x" * (PEER_MESSAGE_MAX_CHARS + 1)
    result, body = await _steer(world, "ses-b", instruction=long_text, ctx=_ctx())
    assert result.is_error and body["type"] == "bad-request" and "32000" in body["message"].replace(",", "")
    await _assert_nothing_stored(world)
    result, body = await _steer(world, "ses-b", instruction=long_text)  # a person's call: no cap, as before
    assert not result.is_error and body["delivery"] == "woken", result.output


# ---- the lead's rulings that are keyed on a peer message ------------------------------------------------------------------------------


async def test_r1_a_steer_from_a_session_never_evicts_a_persons_queued_steer():
    world = await _world()
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running", turn_no=1, created_at=T0 + timedelta(minutes=5))
    await _fill_queue(world)
    queued_before = sorted(p.id for p in await _pending(world))
    result, body = await _steer(world, "ses-b", ctx=_ctx())
    assert result.is_error and body == {"type": "conflict", "message": REFUSE_QUEUE_FULL.format(session_id="ses-b")}
    assert sorted(p.id for p in await _pending(world)) == queued_before
    assert await _ledger(world) == [] and await _events(world, "session.") == []
    # a person's steer (no calling session) still takes the room, as before: the oldest queued row goes
    result, body = await _steer(world, "ses-b")
    assert not result.is_error and body["delivery"] == "queued", result.output
    assert len(await _pending(world)) == _MAX_PENDING_PER_SESSION and sorted(p.id for p in await _pending(world)) != queued_before


async def test_r4_a_steer_from_a_session_never_reopens_a_session_an_operator_cancelled():
    world = await _world()
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="cancelled", turn_no=2, created_at=T0 + timedelta(minutes=5))
    result, body = await _steer(world, "ses-b", ctx=_ctx())
    assert result.is_error and body == {"type": "conflict", "message": "session 'ses-b' has ended (cancelled) and cannot take a message"}
    row = await world.sessions.get("ses-b")
    assert row.status == SessionStatus.ENDED and records(world, "ses-b") == [] and slot(world, "ses-b").reopened is False
    await _assert_nothing_stored(world)
    # A call with no calling session (the MCP endpoint) does not reopen it either: no person stands behind a tool call (ruling 1 of the G7 fixes;
    # this used to reopen it). A person reopens it through the REST steer or restart route (``human_intent=True``).
    result, body = await _steer(world, "ses-b")
    assert result.is_error and body == {"type": "conflict", "message": "Session 'ses-b' has ended (cancelled) and cannot take a message"}
    from primer.session.steer_delivery import DELIVERED_WOKEN, deliver_steer

    out = await deliver_steer(session_id="ses-b", text=INSTRUCTION, parallelism="queue", human_intent=True, **world.deliver_kwargs())
    assert out.outcome == DELIVERED_WOKEN and (await world.sessions.get("ses-b")).status == SessionStatus.RUNNING


# ---- the scope is one of the guards (lead ruling 3 of the G7 fixes) ------------------------------------------------------------------


async def test_a_steer_from_a_session_outside_the_default_workspace_reaches_only_its_own_workspace():
    """Re-pinned (audit G7-05): this was ``test_a_steer_names_its_own_workspace_and_is_not_limited_to_the_senders`` and pinned the delivery to a
    session of another workspace that ``message_session`` refused. It answers exactly as an id that does not exist, and nothing is written."""
    world = await _world()
    await seed_session(world, sid="ses-x", workspace_id="ws-2", turn_no=1, created_at=T0 + timedelta(minutes=5))
    result, body = await _steer(world, "ses-x", workspace_id="ws-2", ctx=_ctx())
    assert result.is_error and body == {"type": "not-found", "message": "Session 'ses-x' does not exist on workspace 'ws-2'"}
    _, unknown = await _steer(world, "ses-nope", workspace_id="ws-2", ctx=_ctx())
    assert unknown == {"type": "not-found", "message": "Session 'ses-nope' does not exist on workspace 'ws-2'"}
    await _assert_nothing_stored(world, target="ses-x", workspace_id="ws-2")


async def test_a_steer_from_a_session_in_the_default_workspace_reaches_any_workspace():
    """Prime is the management plane: its sessions reach every workspace (design 6.1)."""
    world = make_world(with_bus=True)
    await world.sp.set_default_workspace_id("ws-prime")
    await seed_session(world, sid="ses-p", workspace_id="ws-prime", binding=AgentSessionBinding(agent_id="operator"), initiated_by=USER, created_at=T0)
    await seed_session(world, sid="ses-x", workspace_id="ws-2", turn_no=1, created_at=T0 + timedelta(minutes=5))
    result, body = await _steer(world, "ses-x", workspace_id="ws-2", ctx=_ctx("ses-p", "ws-prime"))
    assert not result.is_error and body["delivery"] == "woken", result.output
    (message,) = await _ledger(world)
    assert (message.from_workspace_id, message.to_workspace_id) == ("ws-prime", "ws-2")
    (user_input,) = [r for r in records(world, "ses-x", workspace_id="ws-2") if r["kind"] == "user_input"]
    assert user_input["payload"]["peer"]["from_workspace_id"] == "ws-prime"


async def test_a_session_that_is_not_on_the_named_workspace_is_still_not_found_before_any_guard():
    world = await _world()
    await seed_session(world, sid="ses-x", workspace_id="ws-2", turn_no=1)
    result, body = await _steer(world, "ses-x", workspace_id="ws-1", ctx=_ctx())
    assert result.is_error
    assert body == {"type": "not-found", "message": "Session 'ses-x' does not exist on workspace 'ws-1'"}
    await _assert_nothing_stored(world, target="ses-x", workspace_id="ws-2")


# ---- a call that is not from a session is unchanged ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ctx",
    [
        pytest.param(None, id="no context (the MCP endpoint)"),
        pytest.param(_ctx(session_id=None, workspace_id=None), id="a context with no session (a chat)"),
    ],
)
async def test_a_steer_with_no_calling_session_is_a_plain_instruction_with_no_sender(ctx):
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, peer_hops=5, created_at=T0 + timedelta(minutes=5))
    result, body = await _steer(world, "ses-b", ctx=ctx)
    assert not result.is_error and body["delivery"] == "woken", result.output
    assert slot(world, "ses-b").appended == [INSTRUCTION]
    (user_input,) = [r for r in records(world, "ses-b") if r["kind"] == "user_input"]
    assert "peer" not in user_input["payload"]
    assert (await world.sessions.get("ses-b")).peer_hops == 0, "a person's message starts a fresh chain"
    assert await _ledger(world) == [] and await _events(world, "session.messaged") == []
    (event,) = await _events(world, "session.steered")
    assert "from_session_id" not in event.payload
