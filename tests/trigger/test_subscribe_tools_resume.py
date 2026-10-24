"""The subscribe tools resume the session they parked (ticket 01a16ddb, lead ruling 2), end to end on SQLite.

``workspace_ext__subscribe_to_trigger`` and ``workspace_ext__subscribe_to_channel_event`` park under their own tool names, and no resume hook was
registered for either: whatever woke the park (the fire, the yield cap, a person's cancel), the worker's resume raised ``ConfigError`` ("no resume
hook registered") and answered the call ``{"is_error": true, "reason": "resume failed: ..."}``. They now resume as ``system__wait_for_event`` does:

* on a fire, with what the fire delivered (``{ok, fire_context, payload}``, the ``parked_session`` dispatcher's result);
* on the yield cap, ``{timed_out: true, elapsed_seconds, trigger_id}``;
* on a cancel, ``{cancelled: true, cancel_reason}``.

Every step is the production one: the real tool handler parks (``YieldToWorker``), the session claim adapter writes the park (``ParkRequest``),
``fire_trigger`` and the ``parked_session`` dispatcher publish the wake, the ``YieldEventListener`` flips the row, the ``TimeoutSweeper`` publishes the
cap, the REST yield-cancel route publishes the cancel, and ``WorkerPool._run_engine_session`` runs the resume. Only the agent executor is a recording
stand-in (it would run the next LLM turn).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.bus.in_memory import InMemoryEventBus
from primer.bus.listener import YieldEventListener
from primer.bus.scheduler_tasks import TimeoutSweeper
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, ParkRequest, ReleaseOutcome
from primer.model.channel import ChannelProviderType
from primer.model.channel_event import ChannelEvent, EventSender, NormalizedEventType
from primer.model.chat import Message, ToolCallPart, ToolResultPart
from primer.model.provider import SqliteConfig
from primer.model.scheduler import WorkerConfig
from primer.model.storage import OffsetPage
from primer.model.trigger import ChannelTriggerConfig, DelayedTriggerConfig, Subscription, Trigger
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolContext, YieldToWorker
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.workspace_ext import build_workspace_ext_toolset
from primer.trigger.dispatch import fire_trigger
from primer.trigger.subscribers import DispatchDeps
from primer.worker.pool import WorkerPool
from primer.worker.yield_runtime import ParkedState, stamp_parked_at

SID, CALL, WS = "sess-sub", "call-sub-1", "ws-1"
TRIGGER, CHANNEL_TRIGGER = "tr-sub", "tr-chan"
#: tool -> (arguments, trigger id)
TOOLS = {
    "subscribe_to_trigger": ({"trigger_id": TRIGGER}, TRIGGER),
    "subscribe_to_channel_event": ({"trigger_id": CHANNEL_TRIGGER, "event_matcher": {"event_type": "message.posted"}}, CHANNEL_TRIGGER),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _async(value):
    return value


class _RecordingExecutor:
    """The resumed agent executor: records what the resume injects (the next LLM turn is not run here)."""

    _tool_manager = None

    def __init__(self) -> None:
        self.injected: list[list[Message]] = []

    async def inject_resume_messages(self, messages) -> None:
        self.injected.append(list(messages))


class _NoopPersist:
    pass


@dataclass
class World:
    sp: SqliteStorageProvider
    bus: InMemoryEventBus
    engine: InMemoryClaimEngine

    @property
    def sessions(self):
        return self.sp.get_storage(WorkspaceSession)


@pytest.fixture
async def world(tmp_path):
    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "subscribe.sqlite")))
    await sp.initialize()
    await sp.get_event_store().ensure_schema()
    bus = InMemoryEventBus()
    await bus.initialize()
    engine = InMemoryClaimEngine(adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=sp.get_storage(WorkspaceSession))})
    listener = YieldEventListener(bus=bus, session_storage=sp.get_storage(WorkspaceSession), engine=engine)
    listener.start()
    await sp.get_storage(Trigger).create(Trigger(
        id=TRIGGER, slug="sub-target", name="t", config=DelayedTriggerConfig(fire_at=_now() + timedelta(hours=1)),
        enabled=True, next_fire_at=_now() + timedelta(hours=1), created_at=_now(),
    ))
    await sp.get_storage(Trigger).create(Trigger(
        id=CHANNEL_TRIGGER, slug="sub-room", name="c", config=ChannelTriggerConfig(provider_id="cp-1"), enabled=True, created_at=_now(),
    ))
    try:
        yield World(sp=sp, bus=bus, engine=engine)
    finally:
        await listener.stop()
        await bus.aclose()
        await sp.aclose()


async def _park(world: World, tool: str, *, expired: bool = False) -> None:
    """The session calls ``tool`` mid-turn: the real handler yields and the claim release writes the park, as ``run_one_session_turn`` does."""
    arguments, _ = TOOLS[tool]
    await world.sessions.create(WorkspaceSession(
        id=SID, workspace_id=WS, binding=AgentSessionBinding(agent_id="ag-1"), status=SessionStatus.RUNNING, created_at=_now(),
    ))
    await world.engine.upsert(ClaimKind.SESSION, SID)
    lease = next(ln for ln in await world.engine.claim_due("wrk-turn", max_count=10) if ln.entity_id == SID)
    with pytest.raises(YieldToWorker) as info:
        await build_workspace_ext_toolset(storage_provider=world.sp).call(
            tool_name=tool, arguments=arguments, ctx=ToolContext(tool_call_id=CALL, session_id=SID, workspace_id=WS),
        )
    parked_at = _now()
    yielded = stamp_parked_at(info.value.yielded, parked_at)
    assert yielded.tool_name == tool
    asked = Message(role="assistant", parts=[ToolCallPart(id=CALL, name=f"workspace_ext__{tool}", arguments=arguments)])
    state = ParkedState(
        yielded=yielded, llm_messages=[asked.model_dump(mode="json")], turn_no=0, started_at=parked_at, tool_call_id=CALL,
    )
    # An expired park stands for one that waited out its cap (the global yield cap: these tools take no timeout).
    until = parked_at - timedelta(seconds=1) if expired else parked_at + timedelta(seconds=3600)
    await world.engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True, park=ParkRequest(
        parked_state=state.to_jsonable(), parked_event_key=yielded.event_key, parked_until=until, parked_at=parked_at,
    )))
    assert (await world.sessions.get(SID)).parked_status == "parked"


async def _resume(world: World, monkeypatch) -> tuple[dict, ToolResultPart]:
    """Wait for the wake to flip the row, then let a worker claim it and run the resume; the parked call's answer."""
    for _ in range(200):
        row = await world.sessions.get(SID)
        if row.parked_status == "resumable":
            break
        await asyncio.sleep(0.01)
    assert row.parked_status == "resumable", "nothing woke the park"
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1), scheduler=None, storage=world.sp,  # type: ignore[arg-type]
        workspace_registry=None, provider_registry=None, engine=world.engine, event_bus=world.bus,  # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-resume"
    executor = _RecordingExecutor()
    monkeypatch.setattr(pool, "_load_workspace_for_persist", lambda _ws: _async(_NoopPersist()))
    monkeypatch.setattr(pool, "_build_agent_executor", lambda _s, _w: _async(executor))
    lease = next(ln for ln in await world.engine.claim_due("wrk-resume", max_count=10) if ln.entity_id == SID)
    await pool._run_engine_session(lease)
    assert executor.injected, "the session did not resume"
    [answered] = [part for message in executor.injected[-1] for part in message.parts if isinstance(part, ToolResultPart)]
    assert answered.id == CALL
    assert "resume failed" not in answered.output, answered.output
    assert not answered.error, answered.output
    return json.loads(answered.output), answered


async def _fire(world: World, tool: str):
    _, trigger_id = TOOLS[tool]
    extra = None
    if trigger_id == CHANNEL_TRIGGER:
        extra = {"event": ChannelEvent(
            provider=ChannelProviderType.SLACK, provider_id="cp-1", event_id="ev-approve", type=NormalizedEventType.MESSAGE_POSTED,
            occurred_at=_now(), room_external_id="C1", surface="channel", sender=EventSender(external_id="u-1"), text="approve",
        ).model_dump(mode="json")}
    fired = await fire_trigger(
        trigger_id=trigger_id, scheduled_for=None, deps=DispatchDeps(storage_provider=world.sp, claim_engine=world.engine, event_bus=world.bus), extra_context=extra,
    )
    [delivered] = fired.results
    assert delivered["ok"] is True and delivered["artefact_id"] == SID, delivered
    return fired


@pytest.mark.parametrize("tool", list(TOOLS))
async def test_a_fire_resumes_the_session_with_what_it_delivered(world, monkeypatch, tool):
    await _park(world, tool)

    fired = await _fire(world, tool)
    answer, _ = await _resume(world, monkeypatch)

    _, trigger_id = TOOLS[tool]
    assert answer["ok"] is True
    assert answer["fire_context"]["trigger_id"] == trigger_id
    assert answer["fire_context"]["fire_id"] == fired.fire_id
    # No payload_template on the one-shot subscription: the payload is the fire context itself.
    assert answer["payload"] == answer["fire_context"]
    if tool == "subscribe_to_channel_event":
        assert answer["fire_context"]["event"]["text"] == "approve", "the channel event rides the fire context"
    # One-shot: the fire consumed the parked subscription.
    assert (await world.sp.get_storage(Subscription).list(OffsetPage(offset=0, length=10))).items == []


@pytest.mark.parametrize("tool", list(TOOLS))
async def test_the_yield_cap_resumes_the_session_timed_out(world, monkeypatch, tool):
    await _park(world, tool, expired=True)

    await TimeoutSweeper(bus=world.bus, session_storage=world.sessions)._tick()
    answer, _ = await _resume(world, monkeypatch)

    assert answer["timed_out"] is True
    assert answer["trigger_id"] == TOOLS[tool][1]
    assert answer["elapsed_seconds"] >= 0
    assert "ok" not in answer and "fire_context" not in answer


@pytest.mark.parametrize("tool", list(TOOLS))
async def test_a_cancel_resumes_the_session_cancelled(world, monkeypatch, tool):
    await _park(world, tool)
    registry = ProviderRegistry(
        world.sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(), embedder_factory=lambda p: object(),  # type: ignore[arg-type]
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda p: object(),  # type: ignore[arg-type]
    )
    app = create_test_app(storage_provider=world.sp, provider_registry=registry)  # type: ignore[arg-type]
    app.state.event_bus = world.bus
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await client.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
        cancelled = await client.post(f"/v1/sessions/{SID}/yields/{CALL}/cancel", json={"reason": "not needed any more"})
        assert cancelled.status_code == 202, cancelled.text

    answer, _ = await _resume(world, monkeypatch)

    assert answer == {"cancelled": True, "cancel_reason": "not needed any more"}


@pytest.mark.parametrize("tool", list(TOOLS))
def test_a_canvas_event_that_reaches_the_park_comes_back_without_its_input(tool):
    """The #872 input rule: a ``canvas.event``'s ``value`` and ``data`` never ride a wake.

    A fire never carries one; an envelope that reaches the park's key from the event log is answered as ``wait_for_event`` answers one: the input
    removed (absent, not masked) and every other key kept.
    """
    from primer.worker.yield_resume_registry import ResumeContext, get_resume_hook

    envelope = {
        "event_type": "canvas.event", "session_id": "sess-other", "workspace_id": WS,
        "payload": {"element_id": "pick", "kind": "select", "value": "the user's choice", "data": {"note": "typed"}, "by": "u-1"},
    }
    result = get_resume_hook(tool)(
        {"trigger_id": TOOLS[tool][1]}, envelope, ResumeContext(tool_name=tool, tool_call_id=CALL, session_id=SID),
    )

    assert not result.is_error
    delivered = json.loads(result.output)
    assert delivered["payload"] == {"element_id": "pick", "kind": "select", "by": "u-1"}
    assert delivered["event_type"] == "canvas.event" and delivered["session_id"] == "sess-other"
