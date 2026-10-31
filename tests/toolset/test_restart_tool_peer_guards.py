"""restart_workspace_session called from inside a workspace session is a message from that session (lead ruling 1 of the G7 fixes, audit G7-01).

The tool used to reopen and re-invoke any ENDED session in any workspace with arbitrary input and no guard at all: no rank check, no reach check,
no hop count (the reopened turn started a fresh chain at hop 0), no rate ledger, no attribution, and it reopened a session an operator cancelled.
A user-rank agent at the hop limit could so drive an admin's cancelled session in another workspace, while ``steer_workspace_session`` to the same
target was refused. Called from a session it now goes through ``send_peer_message``, the service ``message_session`` uses: every guard and the
attribution apply, the reopened turn carries the hop count, and a cancelled target is refused (only a person reopens it).

Every call here goes through :func:`primer.agent.inner_dispatch.dispatch_inner_tool`, exactly as ``system__call_tool`` reaches the tool.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from primer.agent.inner_dispatch import dispatch_inner_tool
from primer.mcp.server import current_actor
from primer.model.principal import Principal, PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.workspace_session import AgentSessionBinding, PeerMessage, SessionStatus
from primer.model.yield_ import ToolContext
from primer.session.peer_messages import MAX_PEER_HOPS, PEER_RATE_LIMIT, REFUSE_ENDED, REFUSE_NOT_ENDED
from primer.toolset.workspaces import WORKSPACES_TOOLSET_ID, build_workspaces_toolset
from tests.session._peer_fakes import make_world, records, seed_session, seed_workspace, slot

T0 = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
INPUT = "call_tool crud__delete_agent operator"
USER = PrincipalRef(type="user", id="u1", display="u1", role="user", source="local")
ADMIN = PrincipalRef(type="user", id="u9", display="root", role="admin", source="local")
#: The no-session (MCP) caller's token: its user owns ws-1, so the plain-restart access guard (P2 T6) passes.
TOKEN = Principal(type="user", id="tok-1", display="tok-1", role="user", source="local", user_id="u1")


def _ctx(session_id: str = "ses-a", workspace_id: str = "ws-1", initiated_by: PrincipalRef | None = USER) -> ToolContext:
    return ToolContext(tool_call_id="call_1", session_id=session_id, workspace_id=workspace_id, initiated_by=initiated_by)


async def _world(*, sender_hops: int = 0, sender_workspace: str = "ws-1"):
    """A world with the calling session ``ses-a``: a user's agent session whose turn was started by a message ``sender_hops`` hops from a person."""
    world = make_world(with_bus=True)
    # The access guard (P2 T6) asks the sender's person's full on the TARGET's workspace row: the row must exist and name that person.
    await seed_workspace(world, "ws-1", owners=["u1"])
    await seed_session(
        world, sid="ses-a", workspace_id=sender_workspace, binding=AgentSessionBinding(agent_id="planner"), initiated_by=USER,
        peer_hops=sender_hops, created_at=T0,
    )
    return world


async def _restart(world, session_id: str, *, workspace_id: str = "ws-1", input: str | None = INPUT, ctx: ToolContext | None = None):
    """The tool, reached the way ``system__call_tool`` reaches it."""
    toolset = build_workspaces_toolset(
        storage_provider=world.sp, workspace_registry=world.registry, scheduler=world.scheduler, claim_engine=world.engine, event_bus=world.bus,
    )
    arguments = {"workspace_id": workspace_id, "session_id": session_id}
    if input is not None:
        arguments["input"] = input
    result = await dispatch_inner_tool(
        provider=toolset, toolset_id=WORKSPACES_TOOLSET_ID, tool_name="restart_workspace_session", arguments=arguments, ctx=ctx,
    )
    return result, json.loads(result.output)


async def _ledger(world) -> list[PeerMessage]:
    return list((await world.sp.get_storage(PeerMessage).find(None, OffsetPage(offset=0, length=200))).items)


async def _events(world, prefix: str = "session."):
    return await world.sp.get_event_store().read_after(0, event_type_prefix=prefix)


async def _assert_untouched(world, sid: str, before: dict, *, workspace_id: str = "ws-1") -> None:
    """Nothing was written for a refused restart: the row, the transcript, the slot, the scheduler, the ledger and the events are as they were."""
    assert (await world.sessions.get(sid)).model_dump(mode="json") == before
    assert records(world, sid, workspace_id=workspace_id) == []
    assert slot(world, sid, workspace_id=workspace_id).reopened is False and slot(world, sid, workspace_id=workspace_id).appended == []
    assert world.scheduler.enqueued == [] and world.engine.upserts == []
    assert await _ledger(world) == [] and await _events(world) == []


# ---- the reviewer's scenario -------------------------------------------------------------------------------------------------------


async def test_a_hop_limited_user_agent_cannot_restart_an_admins_cancelled_session_in_another_workspace():
    """Audit G7-01: every guard the restart bypassed would refuse this call; the first one that applies (the access level) answers it, as for an unknown id."""
    world = await _world(sender_hops=MAX_PEER_HOPS)
    target = await seed_session(
        world, sid="ses-x", workspace_id="ws-2", status=SessionStatus.ENDED, ended_reason="cancelled", initiated_by=ADMIN, turn_no=2, created_at=T0,
    )
    before = target.model_dump(mode="json")

    result, body = await _restart(world, "ses-x", workspace_id="ws-2", ctx=_ctx())

    assert result.is_error
    assert body == {"type": "not-found", "message": "Session 'ses-x' does not exist on workspace 'ws-2'"}
    unknown_result, unknown = await _restart(world, "ses-nope", workspace_id="ws-2", ctx=_ctx())
    assert unknown_result.is_error and unknown == {"type": "not-found", "message": "Session 'ses-nope' does not exist on workspace 'ws-2'"}
    await _assert_untouched(world, "ses-x", before, workspace_id="ws-2")


@pytest.mark.parametrize(
    ("sender_hops", "target_ws", "target_fields", "expected"),
    [
        pytest.param(0, "ws-2", {"ended_reason": "completed", "initiated_by": ADMIN}, "not-found",
                     id="access: the sender holds no level on the target's workspace"),
        pytest.param(0, "ws-2", {"ended_reason": "completed", "initiated_by": ADMIN}, "forbidden",
                     id="access: the sender holds only read on the target's workspace"),
        pytest.param(MAX_PEER_HOPS, "ws-1", {"ended_reason": "completed", "initiated_by": USER}, "hop-limit",
                     id="hops: the sender is at the limit"),
        pytest.param(0, "ws-1", {"ended_reason": "cancelled", "initiated_by": USER}, "conflict",
                     id="R4: an operator cancelled it"),
    ],
)
async def test_each_guard_the_restart_bypassed_refuses_it_on_its_own(sender_hops, target_ws, target_fields, expected):
    world = await _world(sender_hops=sender_hops)
    if expected == "not-found":
        # The target's workspace, owned by the admin: the sender (u1) holds no level on it, so it answers as an unknown id.
        await seed_workspace(world, "ws-2", owners=["u9"])
    elif expected == "forbidden":
        # The same workspace, shared with the sender at read only: the level is not enough, and the id is confirmed as existing.
        await seed_workspace(world, "ws-2", owners=["u9"], grants={"u1": "read"})
    target = await seed_session(world, sid="ses-b", workspace_id=target_ws, status=SessionStatus.ENDED, turn_no=2, created_at=T0, **target_fields)
    before = target.model_dump(mode="json")

    result, body = await _restart(world, "ses-b", workspace_id=target_ws, ctx=_ctx())

    assert result.is_error and body["type"] == expected, result.output
    if expected == "not-found":
        assert body["message"] == "Session 'ses-b' does not exist on workspace 'ws-2'"
    if expected == "forbidden":
        assert body["message"] == "you hold less than full access to workspace 'ws-2', so you cannot message its sessions"
    if expected == "hop-limit":
        assert f"{MAX_PEER_HOPS} hops" in body["message"]
    if expected == "conflict":
        assert body["message"] == REFUSE_ENDED.format(session_id="ses-b", ended_reason="cancelled")
    await _assert_untouched(world, "ses-b", before, workspace_id=target_ws)


# ---- a restart from a session is a message from it ---------------------------------------------------------------------------------


async def test_a_restart_from_a_session_reopens_the_target_with_the_senders_attribution_and_hop_count():
    world = await _world(sender_hops=3)
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_no=2, initiated_by=USER, created_at=T0)
    row_keys = set((await world.sessions.get("ses-b")).model_dump(mode="json"))

    result, body = await _restart(world, "ses-b", ctx=_ctx())

    assert not result.is_error, result.output
    assert set(body) == row_keys, "the answer is still the re-armed session row"
    assert (body["status"], body["turn_status"], body["metadata"]["invocation"]) == ("running", "claimable", 2)
    prefixed = f"[from session ses-a (agent planner)]\n{INPUT}"
    assert slot(world, "ses-b").reopened is True and slot(world, "ses-b").appended == [prefixed]
    kinds = [r["kind"] for r in records(world, "ses-b")]
    assert kinds == ["invocation_divider", "user_input"]
    (user_input,) = [r for r in records(world, "ses-b") if r["kind"] == "user_input"]
    (message,) = await _ledger(world)
    assert user_input["payload"]["text"] == prefixed
    assert user_input["payload"]["peer"] == {
        "from_session_id": "ses-a", "from_workspace_id": "ws-1", "from_agent_id": "planner", "from_graph_id": None, "hops": 4,
        "message_id": message.id,
    }
    assert (await world.sessions.get("ses-b")).peer_hops == 4, "the reopened turn carries the chain's hop count, not a fresh 0"
    assert (message.from_session_id, message.to_session_id, message.hops, message.delivery) == ("ses-a", "ses-b", 4, "woken")
    (event,) = await _events(world, "session.messaged")
    assert (event.session_id, event.actor, event.payload["from_session_id"], event.payload["delivery"]) == ("ses-b", "user:u1", "ses-a", "woken")
    assert INPUT not in json.dumps(event.payload)
    assert world.scheduler.enqueued == ["ses-b"]


async def test_two_sessions_restarting_each_other_stop_at_the_hop_limit():
    """The loop the audit names: A restarts B, B ends, B restarts A... The count used to restart at 0 on every restart; now it climbs and stops."""
    world = make_world(with_bus=True)
    await seed_workspace(world, "ws-1", owners=["u1"])
    await seed_session(world, sid="ses-a", binding=AgentSessionBinding(agent_id="planner"), initiated_by=USER, created_at=T0)
    await seed_session(world, sid="ses-b", binding=AgentSessionBinding(agent_id="coder"), initiated_by=USER, created_at=T0)
    sender, target = "ses-a", "ses-b"
    for hop in range(1, MAX_PEER_HOPS + 1):
        row = await world.sessions.get(target)
        await world.sessions.update(row.model_copy(update={"status": SessionStatus.ENDED, "ended_reason": "completed", "turn_status": "idle"}))
        result, body = await _restart(world, target, ctx=_ctx(session_id=sender))
        assert not result.is_error, (hop, result.output)
        assert (await world.sessions.get(target)).peer_hops == hop
        sender, target = target, sender
    row = await world.sessions.get(target)
    await world.sessions.update(row.model_copy(update={"status": SessionStatus.ENDED, "ended_reason": "completed", "turn_status": "idle"}))
    result, body = await _restart(world, target, ctx=_ctx(session_id=sender))
    assert result.is_error and body["type"] == "hop-limit"


async def test_a_restart_and_a_message_share_the_senders_allowance():
    world = await _world()
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_no=2, initiated_by=USER, created_at=T0)
    for i in range(PEER_RATE_LIMIT):
        await world.sp.get_storage(PeerMessage).create(PeerMessage(
            from_session_id="ses-a", from_workspace_id="ws-1", to_session_id="ses-c", to_workspace_id="ws-1", hops=1, delivery="woken",
            sent_at=datetime.now(UTC) - timedelta(seconds=i),
        ))
    result, body = await _restart(world, "ses-b", ctx=_ctx())
    assert result.is_error and body["type"] == "rate-limited", result.output
    assert (await world.sessions.get("ses-b")).status == SessionStatus.ENDED and records(world, "ses-b") == []


async def test_a_session_in_one_workspace_may_restart_a_session_in_a_workspace_its_person_writes():
    """The prime-reaches-everything rule is gone (access-control P2 T6): what decides it is the sender's person's level on the target's
    workspace, and the sender's own workspace being the default one changes nothing."""
    world = await _world(sender_workspace="ws-prime")
    await seed_workspace(world, "ws-2", owners=["u1"])  # the sender's person (u1) holds full on the target's workspace
    await seed_session(
        world, sid="ses-x", workspace_id="ws-2", status=SessionStatus.ENDED, ended_reason="failed", turn_no=2, initiated_by=USER, created_at=T0,
    )
    result, body = await _restart(world, "ses-x", workspace_id="ws-2", ctx=_ctx(workspace_id="ws-prime"))
    assert not result.is_error, result.output
    assert body["status"] == "running"
    (user_input,) = [r for r in records(world, "ses-x", workspace_id="ws-2") if r["kind"] == "user_input"]
    assert user_input["payload"]["peer"]["from_workspace_id"] == "ws-prime"


async def test_a_restart_from_a_session_needs_input():
    world = await _world()
    target = await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_no=2, created_at=T0)
    before = target.model_dump(mode="json")
    result, body = await _restart(world, "ses-b", input=None, ctx=_ctx())
    assert result.is_error and body["type"] == "bad-request"
    assert "give input (1 to 32,000 characters)" in body["message"]
    await _assert_untouched(world, "ses-b", before)


async def test_a_restart_from_a_session_of_a_session_that_has_not_ended_is_a_conflict():
    world = await _world()
    target = await seed_session(world, sid="ses-b", status=SessionStatus.WAITING, turn_no=1, created_at=T0)
    before = target.model_dump(mode="json")
    result, body = await _restart(world, "ses-b", ctx=_ctx())
    assert result.is_error
    assert body == {"type": "conflict", "message": REFUSE_NOT_ENDED.format(session_id="ses-b", status="waiting")}
    await _assert_untouched(world, "ses-b", before)


async def test_a_session_cannot_restart_itself():
    world = await _world()
    result, body = await _restart(world, "ses-a", ctx=_ctx())
    assert result.is_error and body == {"type": "bad-request", "message": "a session cannot message itself"}


# ---- a call with no calling session (the MCP endpoint) -----------------------------------------------------------------------------


async def test_a_restart_with_no_calling_session_is_a_plain_restart_that_never_reopens_a_cancelled_session():
    """No session stands behind a no-session tool call: an operator's Cancel sticks against it (only the REST Restart reopens). The call still
    acts as its token's user (access-control P2 T6), so the plain-restart access guard asks that user's full on the workspace. Any other ended
    session restarts as before: no prefix, no sender, a fresh chain."""
    world = make_world(with_bus=True)
    await seed_workspace(world, "ws-1", owners=["u1"])
    cancelled = await seed_session(world, sid="ses-c", status=SessionStatus.ENDED, ended_reason="cancelled", turn_no=2, created_at=T0)
    before = cancelled.model_dump(mode="json")
    token = current_actor.set(TOKEN)
    try:
        result, body = await _restart(world, "ses-c")
    finally:
        current_actor.reset(token)
    assert result.is_error and body["type"] == "conflict" and "cancelled" in body["message"]
    await _assert_untouched(world, "ses-c", before)

    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_no=2, peer_hops=5, created_at=T0)
    token = current_actor.set(TOKEN)
    try:
        result, body = await _restart(world, "ses-b", input="run again")
    finally:
        current_actor.reset(token)
    assert not result.is_error and body["status"] == "running", result.output
    (user_input,) = [r for r in records(world, "ses-b") if r["kind"] == "user_input"]
    assert user_input["payload"] == {"text": "run again"}
    assert (await world.sessions.get("ses-b")).peer_hops == 0
    assert await _ledger(world) == []
