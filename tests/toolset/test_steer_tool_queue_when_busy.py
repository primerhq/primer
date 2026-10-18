"""steer_workspace_session follows the queue-when-busy rule and emits ``session.steered`` (pivot P3, plan task 3).

The tool used to call ``wake_session`` directly, so a steer to a session that had a turn open wrote a SECOND user message (and pulsed the
scheduler) instead of queueing, and the one action emitted no event while the REST steer route did. It now goes through ``deliver_steer``, the
path every other machine entrypoint uses. The tests run the real tool over the in-memory world of ``tests/session/_peer_fakes.py`` and walk every
state a target session can be in.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from primer.events.recorder import EVENTS_APPENDED_KEY
from primer.model.except_ import ConflictError
from primer.model.principal import Principal, PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.workspace_session import GraphSessionBinding, PendingSessionMessage, SessionStatus
from primer.model.yield_ import ToolContext
from primer.toolset.workspaces import build_workspaces_toolset
from tests.session._peer_fakes import make_world, records, seed_session, slot
from primer.workspace.local.state import _GitCommandError
from tests.toolset._desc_conformance import assert_tool_conforms

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
INSTRUCTION = "Focus on the failing test first."
USER = PrincipalRef(type="user", id="u1", display="u1", role="user", source="local")


def _toolset(world):
    return build_workspaces_toolset(
        storage_provider=world.sp,
        workspace_registry=world.registry,
        scheduler=world.scheduler,
        claim_engine=world.engine,
        event_bus=world.bus,
    )


async def _steer(world, session_id: str, *, workspace_id: str = "ws-1", instruction: str = INSTRUCTION, ctx: ToolContext | None = None):
    result = await _toolset(world).call(
        tool_name="steer_workspace_session",
        arguments={"workspace_id": workspace_id, "session_id": session_id, "instruction": instruction},
        ctx=ctx,
    )
    return result, json.loads(result.output)


async def _steered_events(world):
    return await world.sp.get_event_store().read_after(0, event_type_prefix="session.steered")


async def _pending(world) -> list[PendingSessionMessage]:
    return list((await world.sp.get_storage(PendingSessionMessage).find(None, OffsetPage(offset=0, length=50))).items)


def _kinds(world, session_id: str) -> list[str]:
    return [r["kind"] for r in records(world, session_id)]


def _woken_keys(world) -> list[str]:
    """Every key something was published on, except the recorder's own ``events_appended`` nudge to the event dispatcher (emitting
    ``session.steered`` sends it). A gate or a wait is answered by a publish on ITS key, so this must stay empty for a queued steer."""
    return [key for key, _ in world.bus.published if key != EVENTS_APPENDED_KEY]


# ---- a session with a turn open --------------------------------------------------------------------------------------------------


async def test_steering_a_running_session_queues_it_instead_of_a_second_turn():
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running", turn_no=1)
    result, body = await _steer(world, "ses-b")
    assert not result.is_error, result.output
    assert body["delivery"] == "queued"
    pending = await _pending(world)
    assert [p.parts for p in pending] == [[{"type": "text", "text": INSTRUCTION}]]
    assert world.scheduler.enqueued == [] and world.engine.upserts == []
    assert "user_input" not in _kinds(world, "ses-b") and slot(world, "ses-b").appended == []
    events = await _steered_events(world)
    assert [(e.session_id, e.payload["delivery"]) for e in events] == [("ses-b", "queued")]


@pytest.mark.parametrize(
    ("key", "gate"),
    [("ask_user:ses-b:call_q", "ask_user"), ("tool_approval:ses-b:call_t", "approval")],
)
async def test_steering_a_session_parked_on_a_gate_never_answers_it(key, gate):
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", turn_no=1, parked_status="parked", parked_event_key=key, parked_at=NOW)
    result, body = await _steer(world, "ses-b")
    assert not result.is_error, result.output
    assert body["delivery"] == "queued"
    assert (body["parked_status"], body["parked_event_key"]) == ("parked", key)
    row = await world.sessions.get("ses-b")
    assert (row.parked_status, row.parked_event_key, row.turn_status) == ("parked", key, "idle")
    assert "user_input" not in _kinds(world, "ses-b")
    assert _woken_keys(world) == [], f"the {gate} gate was answered"
    assert world.scheduler.enqueued == [] and len(await _pending(world)) == 1


async def test_steering_an_idle_session_wakes_it_and_emits_session_steered():
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", turn_no=1)
    result, body = await _steer(world, "ses-b")
    assert not result.is_error, result.output
    assert (body["status"], body["turn_status"], body["delivery"]) == ("running", "claimable", "woken")
    assert slot(world, "ses-b").appended == [INSTRUCTION]
    assert world.scheduler.enqueued == ["ses-b"]
    assert await _pending(world) == []
    events = await _steered_events(world)
    assert [(e.session_id, e.payload["delivery"]) for e in events] == [("ses-b", "woken")]


async def test_steering_a_session_of_another_workspace_is_not_found():
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-x", workspace_id="ws-2", turn_no=1)
    result, body = await _steer(world, "ses-x", workspace_id="ws-1")
    assert result.is_error
    assert body == {"type": "not-found", "message": "Session 'ses-x' does not exist on workspace 'ws-1'"}
    assert await _pending(world) == [] and records(world, "ses-x", workspace_id="ws-2") == []
    assert world.scheduler.enqueued == [] and await _steered_events(world) == []
    assert (await world.sessions.get("ses-x")).turn_status == "idle"


async def test_steering_a_session_that_does_not_exist_is_not_found():
    world = make_world(with_bus=True)
    result, body = await _steer(world, "nope")
    assert result.is_error
    assert body == {"type": "not-found", "message": "Session 'nope' does not exist on workspace 'ws-1'"}
    assert await _steered_events(world) == []


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param({"status": SessionStatus.RUNNING, "turn_status": "claimable"}, id="turn armed"),
        pytest.param(
            {"status": SessionStatus.RUNNING, "turn_status": "running", "pause_requested": True}, id="pause requested while running",
        ),
        pytest.param({"parked_status": "parked", "parked_event_key": "evwait:ses-b:call_w", "parked_at": NOW}, id="parked on wait_for_event"),
        pytest.param(
            {"parked_status": "parked", "parked_event_keys": ["evwait:ses-b:call_w", "timer:ses-b:call_t"], "parked_at": NOW},
            id="parked on several events",
        ),
        pytest.param(
            {"parked_status": "resumable", "parked_event_key": "ask_user:ses-b:call_q", "parked_at": NOW}, id="park fired, not yet resumed",
        ),
    ],
)
async def test_every_other_state_with_a_turn_open_queues_and_changes_nothing_else(seed):
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", turn_no=1, **seed)
    before = (await world.sessions.get("ses-b")).model_dump(mode="json")
    result, body = await _steer(world, "ses-b")
    assert not result.is_error, result.output
    assert body["delivery"] == "queued"
    assert {k: v for k, v in body.items() if k != "delivery"} == before
    assert (await world.sessions.get("ses-b")).model_dump(mode="json") == before
    assert len(await _pending(world)) == 1 and records(world, "ses-b") == []
    assert world.scheduler.enqueued == [] and world.engine.upserts == [] and _woken_keys(world) == []
    assert [e.payload["delivery"] for e in await _steered_events(world)] == ["queued"]


# ---- a paused session -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param({}, id="paused while idle (the pause route sets no flag)"),
        pytest.param({"pause_requested": True}, id="paused after the worker honoured the flag"),
    ],
)
async def test_steering_a_paused_session_queues_the_instruction_and_leaves_it_paused(seed):
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", status=SessionStatus.PAUSED, turn_no=1, **seed)
    result, body = await _steer(world, "ses-b")
    assert not result.is_error, result.output
    assert (body["status"], body["delivery"]) == ("paused", "queued")
    assert body["turn_status"] != "claimable"
    row = await world.sessions.get("ses-b")
    assert (row.status, row.pause_requested) == (SessionStatus.PAUSED, bool(seed))
    pending = await _pending(world)
    assert [p.parts for p in pending] == [[{"type": "text", "text": INSTRUCTION}]]
    superseded = [r for r in records(world, "ses-b") if r["kind"] == "pause_superseded"]
    assert [(r["payload"]["action"], r["payload"]["pending_id"]) for r in superseded] == [("queued", pending[0].id)]
    assert "user_input" not in _kinds(world, "ses-b") and slot(world, "ses-b").appended == []
    assert world.scheduler.enqueued == [] and world.engine.upserts == []
    assert [e.payload["delivery"] for e in await _steered_events(world)] == ["queued"]


# ---- an ended session -------------------------------------------------------------------------------------------------------------


async def test_steering_an_ended_restartable_session_reopens_it():
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_no=2, metadata={"invocation": 1})
    result, body = await _steer(world, "ses-b")
    assert not result.is_error, result.output
    assert (body["status"], body["turn_status"], body["delivery"], body["metadata"]["invocation"]) == ("running", "claimable", "woken", 2)
    assert _kinds(world, "ses-b") == ["invocation_divider", "user_input"]
    assert slot(world, "ses-b").reopened is True and slot(world, "ses-b").appended == [INSTRUCTION]
    assert [e.payload["delivery"] for e in await _steered_events(world)] == ["woken"]


@pytest.mark.parametrize(
    ("ended_reason", "phase", "ok"),
    [("force_deleted", "running", False), ("workspace_lost", "ready", False), ("workspace_lost", "running", True)],
)
async def test_steering_an_ended_session_that_cannot_reopen_is_a_conflict_and_stores_nothing(ended_reason, phase, ok):
    world = make_world(with_bus=True)
    world.registry.phase = phase
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason=ended_reason, turn_no=2)
    result, body = await _steer(world, "ses-b")
    if ok:
        assert not result.is_error, result.output
        assert body["delivery"] == "woken"
        return
    assert result.is_error
    assert body == {"type": "conflict", "message": f"Session 'ses-b' has ended ({ended_reason}) and cannot take a message"}
    assert await _pending(world) == [] and records(world, "ses-b") == []
    assert world.scheduler.enqueued == [] and await _steered_events(world) == []
    assert (await world.sessions.get("ses-b")).status == SessionStatus.ENDED


# ---- an ended session that still carries its park ------------------------------------------------------------------------------------
# Cancel of a paused or waiting row and the workspace_lost reconcile end a session and leave its park columns set. A steer must never be queued behind
# that park (nothing would ever deliver it, and the session would stay ended): the old tool called wake_session, which reopens such a session or refuses it.

_STALE_PARKS = {
    "parked": {"parked_status": "parked", "parked_event_key": "ask_user:ses-b:call_q", "parked_at": NOW},
    "resumable": {"parked_status": "resumable", "parked_event_key": "tool_approval:ses-b:call_t", "parked_at": NOW},
}


@pytest.mark.parametrize("park", sorted(_STALE_PARKS))
@pytest.mark.parametrize(
    ("ended_reason", "phase", "reopens"),
    [
        pytest.param("cancelled", "ready", True, id="cancelled"),
        pytest.param("workspace_lost", "running", True, id="workspace lost, the workspace is back"),
        pytest.param("workspace_lost", "ready", False, id="workspace lost, the workspace is down"),
    ],
)
async def test_steering_an_ended_session_with_its_park_still_set_reopens_it_or_refuses_it_and_never_queues(park, ended_reason, phase, reopens):
    world = make_world(with_bus=True)
    world.registry.phase = phase
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason=ended_reason, turn_no=2, **_STALE_PARKS[park])
    result, body = await _steer(world, "ses-b")
    row = await world.sessions.get("ses-b")
    assert await _pending(world) == [], "an ended session has no turn to queue behind"
    assert [key for key in _woken_keys(world) if key == _STALE_PARKS[park]["parked_event_key"]] == [], "a gate was answered"
    if reopens:
        assert not result.is_error, result.output
        assert (body["status"], body["turn_status"], body["delivery"]) == ("running", "claimable", "woken")
        assert (row.parked_status, row.parked_event_key, row.parked_at) == (None, None, None), "the reopen did not clear the park"
        assert slot(world, "ses-b").appended == [INSTRUCTION] and slot(world, "ses-b").reopened is True
        assert [(e.session_id, e.payload["delivery"]) for e in await _steered_events(world)] == [("ses-b", "woken")]
    else:
        assert result.is_error
        assert body == {"type": "conflict", "message": "Session 'ses-b' has ended (workspace_lost) and cannot take a message"}
        assert row.status == SessionStatus.ENDED and records(world, "ses-b") == []
        assert world.scheduler.enqueued == [] and await _steered_events(world) == []


async def test_the_real_cancel_of_a_paused_parked_session_then_a_steer_reopens_it():
    from primer.workspace.session_factory import SessionCancelDeps, cancel_session

    class _Engine:
        async def delete_lease(self, kind, session_id):
            return None

    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", status=SessionStatus.PAUSED, pause_requested=True, turn_no=2, **_STALE_PARKS["resumable"])
    await cancel_session(
        workspace_id="ws-1", session_id="ses-b",
        deps=SessionCancelDeps(storage_provider=world.sp, scheduler=world.scheduler, claim_engine=_Engine(), event_bus=None),
    )
    assert (await world.sessions.get("ses-b")).parked_status == "resumable", "the premise: Cancel keeps the park"
    result, body = await _steer(world, "ses-b")
    assert not result.is_error, result.output
    assert (body["status"], body["delivery"], body["parked_status"]) == ("running", "woken", None)
    assert await _pending(world) == []


@pytest.mark.parametrize(("phase", "reopens"), [("running", True), ("ready", False)])
async def test_the_real_workspace_lost_reconcile_of_a_parked_session_then_a_steer_follows_the_workspace(phase, reopens):
    from primer.workspace.session_reconcile import reconcile_sessions_to_workspace_lost

    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_no=2, **_STALE_PARKS["parked"])
    assert await reconcile_sessions_to_workspace_lost(world.sp, "ws-1") == 1
    assert (await world.sessions.get("ses-b")).parked_status == "parked", "the premise: the reconcile keeps the park"
    world.registry.phase = phase
    result, body = await _steer(world, "ses-b")
    assert await _pending(world) == []
    if reopens:
        assert not result.is_error, result.output
        assert (body["status"], body["delivery"]) == ("running", "woken")
    else:
        assert result.is_error and body["type"] == "conflict"


# ---- the workspace is removed while the steer is delivered -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(lambda: FileNotFoundError(2, "No such file or directory"), id="a filesystem error"),
        pytest.param(
            lambda: _GitCommandError(("add", "--", "sessions/ses-b/messages.jsonl"), 128, "fatal: not a git repository"), id="a git failure",
        ),
    ],
)
async def test_steering_while_the_workspace_is_removed_answers_not_found_with_the_removal_message(failure):
    """The row exists and the workspace goes away under the write: the answer says so, not that the session does not exist."""
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", turn_no=1)

    async def vanish(content, *, extra_parts=None):
        world.registry.remove("ws-1")
        raise failure()

    slot(world, "ses-b").append_instruction = vanish
    result, body = await _steer(world, "ses-b")
    assert result.is_error
    assert body == {"type": "not-found", "message": "workspace 'ws-1' was removed while writing session 'ses-b''s instruction"}
    assert await _pending(world) == [] and await _steered_events(world) == []
    assert world.scheduler.enqueued == [] and (await world.sessions.get("ses-b")).turn_status == "idle"


async def test_a_conflict_from_the_slot_of_a_live_session_is_answered_as_a_conflict_in_the_slots_words():
    """The slot says ENDED while the row does not (the workspace ended its sessions): the refusal is a conflict, and it is not dressed up as the row ending."""
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", turn_no=1)

    async def refuse(content, *, extra_parts=None):
        raise ConflictError("cannot append instruction to ENDED session 'ses-b'")

    slot(world, "ses-b").append_instruction = refuse
    result, body = await _steer(world, "ses-b")
    assert result.is_error
    assert body == {"type": "conflict", "message": "cannot append instruction to ENDED session 'ses-b'"}
    assert await _pending(world) == [] and await _steered_events(world) == []


# ---- a created or graph-bound session, and the answer ------------------------------------------------------------------------------


async def test_steering_a_created_session_starts_it():
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", status=SessionStatus.CREATED)
    result, body = await _steer(world, "ses-b")
    assert not result.is_error, result.output
    assert (body["status"], body["turn_status"], body["delivery"]) == ("running", "claimable", "woken")


async def test_a_graph_session_gets_the_instruction_as_it_is():
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-g", turn_no=1, binding=GraphSessionBinding(graph_id="triage"))
    result, body = await _steer(world, "ses-g")
    assert not result.is_error, result.output
    assert body["delivery"] == "woken"
    assert slot(world, "ses-g").appended == [INSTRUCTION]
    assert [r["payload"]["text"] for r in records(world, "ses-g") if r["kind"] == "user_input"] == [INSTRUCTION]


async def test_the_answer_keeps_every_key_of_the_session_row_and_adds_delivery():
    world = make_world()
    await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running", turn_no=1)
    row_keys = set((await world.sessions.get("ses-b")).model_dump(mode="json"))
    _, body = await _steer(world, "ses-b")
    assert set(body) == row_keys | {"delivery"}
    assert body["id"] == "ses-b" and body["workspace_id"] == "ws-1"


# ---- the event ---------------------------------------------------------------------------------------------------------------------


async def test_session_steered_names_the_target_and_the_caller_and_carries_the_delivery():
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", turn_no=1)
    ctx = ToolContext(tool_call_id="call_1", session_id="ses-a", workspace_id="ws-1", initiated_by=USER)
    result, _ = await _steer(world, "ses-b", ctx=ctx)
    assert not result.is_error, result.output
    (event,) = await _steered_events(world)
    assert event.event_type == "session.steered"
    assert (event.workspace_id, event.session_id, event.actor) == ("ws-1", "ses-b", "user:u1")
    assert event.payload == {"has_instruction": True, "has_tool_results": False, "has_attachments": False, "delivery": "woken"}


async def test_session_steered_is_attributed_to_system_when_no_caller_is_known():
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", turn_no=1)
    await _steer(world, "ses-b")
    await _steer(world, "ses-b", ctx=ToolContext(tool_call_id="call_1", session_id=None, workspace_id=None))
    assert [e.actor for e in await _steered_events(world)] == ["system", "system"]


async def test_session_steered_on_the_mcp_endpoint_is_attributed_to_the_requests_actor():
    """No ToolContext is the MCP endpoint, whose caller is the request's actor (the rule ``_caller_is_admin`` applies)."""
    from primer.mcp.server import current_actor

    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", turn_no=1)
    token = current_actor.set(Principal(type="api_token", id="tok-1", display="ci", role="user", source="internal"))
    try:
        await _steer(world, "ses-b")
    finally:
        current_actor.reset(token)
    assert [e.actor for e in await _steered_events(world)] == ["api_token:tok-1"]


# ---- the tool's declaration -------------------------------------------------------------------------------------------------------


async def test_a_blank_instruction_is_a_validation_error_and_stores_nothing():
    world = make_world(with_bus=True)
    await seed_session(world, sid="ses-b", turn_no=1)
    result, body = await _steer(world, "ses-b", instruction="")
    assert result.is_error and body["type"] == "validation-error"
    assert records(world, "ses-b") == [] and await _steered_events(world) == []


async def test_without_a_scheduler_the_tool_is_unavailable():
    world = make_world()
    toolset = build_workspaces_toolset(
        storage_provider=world.sp, workspace_registry=world.registry, scheduler=None, claim_engine=None, event_bus=None,
    )
    result = await toolset.call(
        tool_name="steer_workspace_session", arguments={"workspace_id": "ws-1", "session_id": "x", "instruction": "y"},
    )
    assert result.is_error and json.loads(result.output)["type"] == "unavailable"


async def test_the_tool_says_what_a_steer_does_for_each_state_and_what_it_returns():
    from primer.mcp.safety import is_exposable

    world = make_world()
    toolset = _toolset(world)
    tools = {t.id: t async for t in toolset.list_tools()}
    tool = tools["steer_workspace_session"]
    assert_tool_conforms(tool)
    assert toolset.required_role("steer_workspace_session") == "user"
    assert tool.interruptible is False and tool.requires_workspace is False
    assert is_exposable(tool, provider=toolset) == (True, None)
    assert set(tool.args_schema["required"]) == {"workspace_id", "session_id", "instruction"}
    for sentence in (
        "Send a user instruction to a session in any workspace: an idle or ended session starts its next turn with it, "
        "a busy, parked or paused one reads it at its next turn boundary.",
        "Use when you want to redirect a session; not to pause or resume it (use pause_workspace_session / "
        "resume_workspace_session) and not to answer its ask_user (a queued instruction is never an answer). "
        "Returns the session row plus delivery: woken or queued.",
    ):
        assert " ".join(sentence.split()) in " ".join(tool.description.split()), sentence
    assert tool.examples[0].returns == 'the session row plus {delivery: "woken"}'
