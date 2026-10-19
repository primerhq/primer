"""The ``list_my_sessions`` and ``message_session`` tools of the ``workspaces`` toolset (pivot P3, plan task 7; design 6.1).

The tools are thin handlers: the guards and the delivery are ``primer.session.peer_messages.send_peer_message`` (tested in
``tests/session/test_send_peer_message.py``), the listing is ``list_workspace_session_summaries``. These tests run the real toolset over the in-memory world of
``tests/session/_peer_fakes.py`` and pin what the handlers own: the calling session comes from the tool context and nowhere else, a call outside a workspace
session is refused, a refusal of the service keeps its error type, the answer carries the service's result, and the declaration (role, workspace-only, not
MCP-exposable, interruptible, copy) is what the plan quotes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from primer.events.recorder import EVENTS_APPENDED_KEY
from primer.mcp.safety import is_exposable
from primer.model.principal import PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.workspace_session import AgentSessionBinding, GraphSessionBinding, PeerMessage, PendingSessionMessage, SessionStatus
from primer.model.yield_ import ToolContext
from primer.session.peer_messages import NOTE_GRAPH, NOTE_WOKEN, PEER_MESSAGE_MAX_CHARS, PEER_RATE_LIMIT
from primer.toolset.workspaces import WORKSPACES_TOOLSET_ID, build_workspaces_toolset
from tests.session._peer_fakes import make_world, records, seed_session, slot
from tests.toolset._desc_conformance import assert_tool_conforms

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
USER = PrincipalRef(type="user", id="u1", display="u1", role="user", source="local")
ADMIN = PrincipalRef(type="user", id="u2", display="u2", role="admin", source="local")

SUMMARY_KEYS = {
    "id", "title", "agent_id", "graph_id", "status", "state", "turn_status", "parked_on", "created_by", "parent_session_id", "last_activity_at", "self",
}
RESULT_KEYS = {
    "message_id", "session_id", "workspace_id", "delivery", "hops", "target_state", "target_parked_on", "restarts_graph", "note",
}


def _toolset(world, **overrides):
    kwargs = {
        "storage_provider": world.sp,
        "workspace_registry": world.registry,
        "scheduler": world.scheduler,
        "claim_engine": world.engine,
        "event_bus": world.bus,
    }
    return build_workspaces_toolset(**{**kwargs, **overrides})


def _ctx(session_id: str = "ses-a", workspace_id: str = "ws-1", initiated_by: PrincipalRef | None = USER) -> ToolContext:
    return ToolContext(tool_call_id="call_1", session_id=session_id, workspace_id=workspace_id, initiated_by=initiated_by)


async def _call(world, tool: str, arguments: dict, *, ctx: ToolContext | None = None, toolset=None):
    result = await (toolset or _toolset(world)).call(tool_name=tool, arguments=arguments, ctx=ctx)
    return result, json.loads(result.output)


async def _world(*, with_bus: bool = True):
    """A world with the caller ``ses-a``: an agent session of ``ws-1`` that rests (so it is not ``running`` for the state filter), started by a user."""
    world = make_world(with_bus=with_bus)
    await seed_session(world, sid="ses-a", binding=AgentSessionBinding(agent_id="planner"), initiated_by=USER, created_at=T0)
    return world


async def _events(world, prefix: str):
    return await world.sp.get_event_store().read_after(0, event_type_prefix=prefix)


async def _pending(world) -> list[PendingSessionMessage]:
    return list((await world.sp.get_storage(PendingSessionMessage).find(None, OffsetPage(offset=0, length=200))).items)


async def _ledger(world) -> list[PeerMessage]:
    return list((await world.sp.get_storage(PeerMessage).find(None, OffsetPage(offset=0, length=200))).items)


# ---- list_my_sessions ------------------------------------------------------------------------------------------------------------


async def test_list_my_sessions_lists_only_the_callers_workspace():
    world = await _world()
    await seed_session(
        world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running", turn_no=1, created_at=T0 + timedelta(minutes=5),
        name="the reviewer", parent_session_id="ses-a",
    )
    await seed_session(world, sid="ses-x", workspace_id="ws-2", created_at=T0 + timedelta(minutes=9))
    result, body = await _call(world, "list_my_sessions", {}, ctx=_ctx())
    assert not result.is_error, result.output
    assert [item["id"] for item in body["items"]] == ["ses-b", "ses-a"]
    assert (body["workspace_id"], body["total"]) == ("ws-1", 2)
    assert set(body["items"][0]) == SUMMARY_KEYS
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id["ses-a"]["self"] is True and by_id["ses-b"]["self"] is False
    assert by_id["ses-b"]["turn_status"] == "running" and by_id["ses-b"]["state"] == "running"
    assert (by_id["ses-b"]["title"], by_id["ses-b"]["parent_session_id"]) == ("the reviewer", "ses-a")
    assert (by_id["ses-a"]["agent_id"], by_id["ses-a"]["graph_id"], by_id["ses-a"]["status"]) == ("planner", None, "waiting")


async def test_list_my_sessions_takes_the_workspace_from_the_context_not_from_an_argument():
    world = await _world()
    await seed_session(world, sid="ses-x", workspace_id="ws-2", created_at=T0 + timedelta(minutes=9))
    result, body = await _call(world, "list_my_sessions", {"workspace_id": "ws-2"}, ctx=_ctx())
    assert not result.is_error, result.output
    assert body["workspace_id"] == "ws-1" and [item["id"] for item in body["items"]] == ["ses-a"]
    # and the other workspace's caller sees its own sessions
    await seed_session(world, sid="ses-y", workspace_id="ws-2", created_at=T0)
    _, other = await _call(world, "list_my_sessions", {}, ctx=_ctx("ses-y", "ws-2"))
    assert other["workspace_id"] == "ws-2" and [item["id"] for item in other["items"]] == ["ses-x", "ses-y"]


async def test_list_my_sessions_filters_by_state_and_caps_the_page():
    world = await _world()
    await seed_session(
        world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running", turn_no=1, created_at=T0 + timedelta(minutes=5),
    )
    _, running = await _call(world, "list_my_sessions", {"state": "running"}, ctx=_ctx())
    assert [item["id"] for item in running["items"]] == ["ses-b"]
    _, capped = await _call(world, "list_my_sessions", {"limit": 1}, ctx=_ctx())
    assert [item["id"] for item in capped["items"]] == ["ses-b"] and capped["total"] == 2


async def test_list_my_sessions_says_what_a_parked_session_waits_on():
    world = await _world()
    await seed_session(
        world, sid="ses-b", turn_no=1, parked_status="parked", parked_event_key="ask_user:ses-b:call_1", parked_at=NOW,
        created_at=T0 + timedelta(minutes=5),
    )
    _, body = await _call(world, "list_my_sessions", {}, ctx=_ctx())
    item = next(i for i in body["items"] if i["id"] == "ses-b")
    assert (item["parked_on"], item["state"]) == ("ask_user", "parked")
    assert next(i for i in body["items"] if i["id"] == "ses-a")["parked_on"] is None


async def test_list_my_sessions_names_the_graph_of_a_graph_session_and_the_creator_of_each():
    world = await _world()
    await seed_session(
        world, sid="ses-g", binding=GraphSessionBinding(graph_id="triage"), initiated_by=ADMIN, created_at=T0 + timedelta(minutes=5),
    )
    await seed_session(world, sid="ses-n", created_at=T0 + timedelta(minutes=6))  # a historical row: no recorded creator
    _, body = await _call(world, "list_my_sessions", {}, ctx=_ctx())
    by_id = {item["id"]: item for item in body["items"]}
    assert (by_id["ses-g"]["agent_id"], by_id["ses-g"]["graph_id"]) == (None, "triage")
    assert by_id["ses-g"]["created_by"] == {"type": "user", "id": "u2", "display": "u2", "role": "admin"}
    assert by_id["ses-a"]["created_by"] == {"type": "user", "id": "u1", "display": "u1", "role": "user"}
    assert by_id["ses-n"]["created_by"] is None


async def test_list_my_sessions_last_activity_is_the_last_turn_else_the_creation_time():
    world = await _world()
    last_turn = T0 + timedelta(hours=2)
    await seed_session(world, sid="ses-b", turn_no=3, last_turn_at=last_turn, created_at=T0 + timedelta(minutes=5))
    _, body = await _call(world, "list_my_sessions", {}, ctx=_ctx())
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id["ses-b"]["last_activity_at"] == last_turn.isoformat()
    assert by_id["ses-a"]["last_activity_at"] == T0.isoformat()


async def test_list_my_sessions_lists_an_ended_session_too_with_its_state():
    world = await _world()
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_no=2, created_at=T0 + timedelta(minutes=5))
    _, body = await _call(world, "list_my_sessions", {}, ctx=_ctx())
    assert next(i for i in body["items"] if i["id"] == "ses-b")["state"] == "ended"
    _, ended = await _call(world, "list_my_sessions", {"state": "ended"}, ctx=_ctx())
    assert [item["id"] for item in ended["items"]] == ["ses-b"]


@pytest.mark.parametrize(("arguments", "field"), [({"state": "sleeping"}, "state"), ({"limit": 0}, "limit"), ({"limit": 201}, "limit")])
async def test_list_my_sessions_rejects_a_bad_argument(arguments, field):
    world = await _world()
    result, body = await _call(world, "list_my_sessions", arguments, ctx=_ctx())
    assert result.is_error and body["type"] == "validation-error" and field in body["message"]


# ---- both tools: only inside a workspace session ----------------------------------------------------------------------------------


_TOOL_ARGUMENTS = {"list_my_sessions": {}, "message_session": {"session_id": "ses-b", "text": "hello"}}


@pytest.mark.parametrize("tool", sorted(_TOOL_ARGUMENTS))
@pytest.mark.parametrize(
    "ctx",
    [
        pytest.param(None, id="no context (what the MCP endpoint dispatches with)"),
        pytest.param(ToolContext(tool_call_id="c", session_id=None, workspace_id=None, chat_id="chat-1", initiated_by=USER), id="a chat"),
        pytest.param(ToolContext(tool_call_id="c", session_id="ses-a", workspace_id=None, initiated_by=USER), id="no workspace"),
        pytest.param(ToolContext(tool_call_id="c", session_id=None, workspace_id="ws-1", initiated_by=USER), id="no session"),
    ],
)
async def test_the_messaging_tools_are_refused_outside_a_workspace_session(tool, ctx):
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, created_at=T0 + timedelta(minutes=5))
    result, body = await _call(world, tool, _TOOL_ARGUMENTS[tool], ctx=ctx)
    assert result.is_error
    assert body == {"type": "unsupported-surface", "message": f"{tool} works only inside a workspace session"}
    assert await _pending(world) == [] and await _ledger(world) == [] and await _events(world, "session.") == []
    assert records(world, "ses-b") == [] and world.scheduler.enqueued == []


# ---- message_session ---------------------------------------------------------------------------------------------------------------


async def test_message_session_delivers_and_answers_with_the_result():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, created_at=T0 + timedelta(minutes=5))
    result, body = await _call(world, "message_session", {"session_id": "ses-b", "text": "ping"}, ctx=_ctx())
    assert not result.is_error, result.output
    assert set(body) == RESULT_KEYS
    assert (body["delivery"], body["hops"], body["note"]) == ("woken", 1, NOTE_WOKEN)
    assert (body["session_id"], body["workspace_id"], body["restarts_graph"]) == ("ses-b", "ws-1", False)
    assert body["message_id"].startswith("pmsg-")
    assert slot(world, "ses-b").appended == ["[from session ses-a (agent planner)]\nping"]
    assert [(r.delivery, r.from_session_id, r.to_session_id) for r in await _ledger(world)] == [("woken", "ses-a", "ses-b")]


async def test_message_session_mode_defaults_to_auto_and_if_idle_is_passed_through():
    world = await _world()
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running", turn_no=1, created_at=T0 + timedelta(minutes=5))
    _, auto = await _call(world, "message_session", {"session_id": "ses-b", "text": "ping"}, ctx=_ctx())
    assert auto["delivery"] == "queued"
    result, body = await _call(world, "message_session", {"session_id": "ses-b", "text": "ping", "mode": "if_idle"}, ctx=_ctx())
    assert result.is_error and body == {
        "type": "conflict", "message": "session 'ses-b' is busy; nothing was sent (mode if_idle)",
    }
    assert len(await _pending(world)) == 1 and len(await _ledger(world)) == 1


async def test_message_session_takes_the_sender_from_the_context_and_never_from_the_arguments():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, created_at=T0 + timedelta(minutes=5))
    await seed_session(world, sid="ses-c", binding=AgentSessionBinding(agent_id="coder"), initiated_by=USER, created_at=T0)
    result, body = await _call(
        world, "message_session", {"session_id": "ses-b", "text": "ping", "sender_session_id": "ses-c", "from_session_id": "ses-c"},
        ctx=_ctx("ses-a"),
    )
    assert not result.is_error, result.output
    assert slot(world, "ses-b").appended == ["[from session ses-a (agent planner)]\nping"]
    (message,) = await _ledger(world)
    assert message.from_session_id == "ses-a"


@pytest.mark.parametrize(
    ("target", "error_type", "message"),
    [
        ("ses-a", "bad-request", "a session cannot message itself"),
        ("nope", "not-found", "Session 'nope' does not exist in this workspace"),
        ("ses-x", "not-found", "Session 'ses-x' does not exist in this workspace"),
    ],
)
async def test_message_session_maps_a_refusal_to_its_error_type(target, error_type, message):
    world = await _world()
    await seed_session(world, sid="ses-x", workspace_id="ws-2", turn_no=1)
    result, body = await _call(world, "message_session", {"session_id": target, "text": "ping"}, ctx=_ctx())
    assert result.is_error
    assert body == {"type": error_type, "message": message}
    assert await _ledger(world) == [] and await _pending(world) == [] and await _events(world, "session.") == []


async def test_message_session_refuses_a_higher_ranked_target_as_forbidden():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, initiated_by=ADMIN, created_at=T0 + timedelta(minutes=5))
    result, body = await _call(world, "message_session", {"session_id": "ses-b", "text": "ping"}, ctx=_ctx())
    assert result.is_error and body["type"] == "forbidden"
    assert "(admin)" in body["message"] and "(user)" in body["message"]
    assert await _ledger(world) == [] and records(world, "ses-b") == []
    # the same message from an admin run is delivered
    admin_run = _ctx(initiated_by=ADMIN)
    result, body = await _call(world, "message_session", {"session_id": "ses-b", "text": "ping"}, ctx=admin_run)
    assert not result.is_error, result.output


async def test_message_session_ranks_a_run_with_no_recorded_initiator_as_an_ordinary_user():
    """The sender's principal is ``run_principal(ctx)``: a context that names no initiator is an ordinary user, not a refused unknown."""
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, created_at=T0 + timedelta(minutes=5))
    await seed_session(world, sid="ses-c", turn_no=1, initiated_by=ADMIN, created_at=T0 + timedelta(minutes=6))
    result, body = await _call(world, "message_session", {"session_id": "ses-b", "text": "ping"}, ctx=_ctx(initiated_by=None))
    assert not result.is_error and body["delivery"] == "woken", result.output
    result, body = await _call(world, "message_session", {"session_id": "ses-c", "text": "ping"}, ctx=_ctx(initiated_by=None))
    assert result.is_error and body["type"] == "forbidden"
    assert "(admin)" in body["message"] and "(user)" in body["message"], "it was ranked, not refused as an unknown sender"


async def test_message_session_stops_a_chain_at_the_hop_limit_and_a_flood_at_the_rate_limit():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, created_at=T0 + timedelta(minutes=5))
    await seed_session(world, sid="ses-h", binding=AgentSessionBinding(agent_id="deep"), initiated_by=USER, peer_hops=8, created_at=T0)
    result, body = await _call(world, "message_session", {"session_id": "ses-b", "text": "ping"}, ctx=_ctx("ses-h"))
    assert result.is_error and body["type"] == "hop-limit"
    assert await _ledger(world) == []
    for i in range(PEER_RATE_LIMIT):
        await world.sp.get_storage(PeerMessage).create(PeerMessage(
            from_session_id="ses-a", from_workspace_id="ws-1", to_session_id="ses-b", to_workspace_id="ws-1", hops=1, delivery="woken",
            sent_at=datetime.now(UTC) - timedelta(seconds=i),
        ))
    result, body = await _call(world, "message_session", {"session_id": "ses-b", "text": "ping"}, ctx=_ctx())
    assert result.is_error and body["type"] == "rate-limited"
    assert "sent 20 messages in the last 10 minutes" in body["message"]
    assert records(world, "ses-b") == [] and await _pending(world) == []


async def test_message_session_text_is_one_to_32000_characters():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, created_at=T0 + timedelta(minutes=5))
    result, body = await _call(world, "message_session", {"session_id": "ses-b", "text": "x" * (PEER_MESSAGE_MAX_CHARS + 1)}, ctx=_ctx())
    assert result.is_error and body["type"] == "validation-error"
    result, body = await _call(world, "message_session", {"session_id": "ses-b", "text": ""}, ctx=_ctx())
    assert result.is_error and body["type"] == "validation-error"
    assert await _ledger(world) == [] and records(world, "ses-b") == []
    result, body = await _call(world, "message_session", {"session_id": "ses-b", "text": "x" * PEER_MESSAGE_MAX_CHARS}, ctx=_ctx())
    assert not result.is_error, result.output


@pytest.mark.parametrize(
    "arguments",
    [{"text": "hi"}, {"session_id": "", "text": "hi"}, {"session_id": "ses-b"}, {"session_id": "ses-b", "text": "hi", "mode": "now"}],
    ids=["no session", "empty session", "no text", "unknown mode"],
)
async def test_message_session_rejects_a_bad_argument_and_stores_nothing(arguments):
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, created_at=T0 + timedelta(minutes=5))
    result, body = await _call(world, "message_session", arguments, ctx=_ctx())
    assert result.is_error and body["type"] == "validation-error"
    assert await _ledger(world) == [] and records(world, "ses-b") == [] and world.scheduler.enqueued == []


async def test_message_session_without_a_scheduler_is_unavailable():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, created_at=T0 + timedelta(minutes=5))
    toolset = _toolset(world, scheduler=None, claim_engine=None)
    result, body = await _call(world, "message_session", {"session_id": "ses-b", "text": "ping"}, ctx=_ctx(), toolset=toolset)
    assert result.is_error
    assert body == {"type": "unavailable", "message": "session tools unavailable: scheduler/claim_engine not wired"}
    assert records(world, "ses-b") == [] and await _ledger(world) == []
    # reading the sessions needs neither
    result, body = await _call(world, "list_my_sessions", {}, ctx=_ctx(), toolset=toolset)
    assert not result.is_error and [item["id"] for item in body["items"]] == ["ses-b", "ses-a"]


async def test_message_session_to_a_graph_session_says_it_restarts_the_graph():
    world = await _world()
    await seed_session(world, sid="ses-g", binding=GraphSessionBinding(graph_id="triage"), turn_no=1, created_at=T0 + timedelta(minutes=5))
    result, body = await _call(world, "message_session", {"session_id": "ses-g", "text": "INC-1"}, ctx=_ctx())
    assert not result.is_error, result.output
    assert body["restarts_graph"] is True and body["note"] == NOTE_WOKEN + NOTE_GRAPH
    assert slot(world, "ses-g").appended == ["INC-1"]


async def test_message_session_attributes_its_events_to_the_calling_run():
    world = await _world()
    await seed_session(world, sid="ses-b", turn_no=1, created_at=T0 + timedelta(minutes=5))
    result, body = await _call(world, "message_session", {"session_id": "ses-b", "text": "ping"}, ctx=_ctx())
    assert not result.is_error, result.output
    (event,) = await _events(world, "session.messaged")
    assert (event.actor, event.session_id, event.workspace_id, event.correlation_id) == ("user:u1", "ses-b", "ws-1", body["message_id"])
    assert (event.payload["from_session_id"], event.payload["mode"], event.payload["delivery"]) == ("ses-a", "auto", "woken")
    assert "ping" not in json.dumps(event.payload)


async def test_message_session_to_a_parked_gate_queues_and_answers_nothing():
    world = await _world()
    key = "ask_user:ses-b:call_q"
    await seed_session(world, sid="ses-b", turn_no=1, parked_status="parked", parked_event_key=key, parked_at=NOW, created_at=T0 + timedelta(minutes=5))
    result, body = await _call(world, "message_session", {"session_id": "ses-b", "text": "here is your answer"}, ctx=_ctx())
    assert not result.is_error, result.output
    assert (body["delivery"], body["target_parked_on"], body["target_state"]) == ("queued", "ask_user", "parked")
    assert [key for key, _ in world.bus.published if key != EVENTS_APPENDED_KEY] == []
    row = await world.sessions.get("ses-b")
    assert (row.parked_status, row.parked_event_key) == ("parked", key)
    assert "user_input" not in [r["kind"] for r in records(world, "ses-b")]


# ---- the declaration ---------------------------------------------------------------------------------------------------------------


async def test_the_messaging_tools_are_workspace_only_and_not_mcp_exposable():
    world = make_world()
    toolset = _toolset(world)
    tools = {t.id: t async for t in toolset.list_tools()}
    for name in ("list_my_sessions", "message_session"):
        tool = tools[name]
        assert tool.toolset_id == WORKSPACES_TOOLSET_ID
        assert tool.requires_workspace is True and tool.required_role == "user"
        assert toolset.required_role(name) == "user" and toolset.requires_workspace(name) is True
        assert_tool_conforms(tool)
        assert is_exposable(tool, provider=toolset) == (False, "needs_workspace")
    assert tools["message_session"].interruptible is False
    assert tools["list_my_sessions"].interruptible is True


async def test_the_messaging_tools_say_what_the_plan_says():
    world = make_world()
    tools = {t.id: t async for t in _toolset(world).list_tools()}
    listing, sending = tools["list_my_sessions"], tools["message_session"]

    def flat(text: str) -> str:
        return " ".join(text.split())

    assert flat(
        "List the sessions in your own workspace with their state, so you can pick one to message."
    ) in flat(listing.description)
    assert flat(
        "Use when you need the ids, agents and states of the other sessions in your workspace, for example before message_session; "
        "not for another workspace (use list_workspace_sessions) and not for one session's transcript (use read_workspace_session_messages). "
        "Each item says what a parked session waits on (parked_on) and marks your own session with self: true."
    ) in flat(listing.description)
    assert listing.examples[0].args == {} and listing.examples[0].returns == (
        "{workspace_id, items: [{id, title, agent_id, graph_id, status, state, turn_status, parked_on, created_by, parent_session_id, "
        "last_activity_at, self}], total}"
    )
    assert listing.examples[1].args == {"state": "running", "limit": 10}
    assert set(listing.args_schema["properties"]) == {"state", "limit"} and not listing.args_schema.get("required")

    assert flat(
        "Send a message to another session: an idle session starts its next turn with it, a busy one reads it at its next turn boundary."
    ) in flat(sending.description)
    assert flat(
        "Use when another session's agent should act on something or learn a result; not for large outputs (write a file and send its path) "
        "and not to answer a session's ask_user (a message is never an answer). "
        "The target sees a user message that starts with [from session <your session id> (agent <your agent id>)]. "
        "Refusals: bad-request (yourself), not-found (no such session in your workspace), forbidden (it runs at a higher rank than you), "
        "hop-limit (a chain of 8 messages), rate-limited (20 messages per 10 minutes), "
        "conflict (mode if_idle and the session is busy, or an ended session that cannot reopen)."
    ) in flat(sending.description)
    assert sending.examples[0].args == {"session_id": "ses-2", "text": "The API spec is in docs/api.md; build the client against it."}
    assert sending.examples[0].returns == (
        '{message_id, session_id, workspace_id, delivery: "woken", hops: 1, target_state, target_parked_on, restarts_graph, note}'
    )
    assert sending.examples[1].args == {"session_id": "ses-3", "text": "Pick up the next ticket when you are free.", "mode": "if_idle"}
    assert set(sending.args_schema["required"]) == {"session_id", "text"}
    assert sending.args_schema["properties"]["text"]["maxLength"] == PEER_MESSAGE_MAX_CHARS
    assert sending.args_schema["properties"]["mode"]["enum"] == ["auto", "if_idle"]
