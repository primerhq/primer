"""canvas_wait: the pausing tool (pivot P5, Task 10; plan Decision 8; Review Focus 1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio

from primer.canvas.lang import parse_program
from primer.canvas.wait import (
    CANVAS_WAIT_PREFIX,
    CANVAS_WAIT_TOOL,
    canvas_wait_event_key,
    canvas_wait_filter,
    canvas_wait_matches,
    consume_events,
    drop_queued_steers,
)
from primer.events.dispatcher import EventDispatcher
from primer.events.filters import matches
from primer.model.canvas import CANVAS_EVENT_KINDS
from primer.model.event import Event, EventSubscription, SessionWakeSink
from primer.model.storage import OffsetPage
from primer.model.workspace_session import PendingSessionMessage, SessionStatus
from primer.model.yield_ import ToolContext, YieldCancelled, YieldTimeout, YieldToWorker, Yielded
from primer.toolset.canvas import build_canvas_toolset, canvas_wait_resume
from primer.toolset.tool_catalog import collect_tool_entries
from primer.worker.yield_resume_registry import ResumeContext, get_resume_hook
from tests.canvas._fakes import World, make_world, seed_session
from tests.docs._live_toolsets import built_in_providers, built_in_tools

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)

PROGRAM = (
    'root = Column([ok, cancel, genre, player, viz])\nok = Button("OK", "primary")\ncancel = Button("Cancel")\n'
    'genre = Select("Genre", ["Jazz", "Lo-fi"], "Jazz")\nplayer = Audio("https://cdn.example.com/a.mp3", "Song")\nviz = Html("<b>x</b>", 60)'
)


@pytest_asyncio.fixture
async def world() -> World:
    w = make_world()
    await seed_session(w)
    await w.service.apply(
        session_id="s1", workspace_id="ws-1", statements=parse_program(PROGRAM), unit="line", base_rev=None, source="agent", by="agent",
    )
    return w


@pytest.fixture
def provider(world: World):
    return build_canvas_toolset(storage_provider=world.sp, event_bus=world.bus, workspace_registry=world.registry)


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(tool_call_id="call-1", session_id="s1", workspace_id="ws-1")


def resume_context(provider, session_id: str | None = "s1", *, resolve=True) -> ResumeContext:
    async def resolve_provider(toolset_id: str):
        assert toolset_id == "canvas"
        return provider

    return ResumeContext(
        tool_name=CANVAS_WAIT_TOOL, tool_call_id="call-1", session_id=session_id, resolve_provider=resolve_provider if resolve else None,
    )


async def _event(world: World, element: str, kind: str, **kwargs: Any):
    return await world.service.record_event(session_id="s1", workspace_id="ws-1", element=element, kind=kind, by="alice", **kwargs)


async def _park(world: World, yielded: Yielded) -> None:
    """The park row the worker writes after the tool returns: what the dispatcher looks for before it delivers."""
    row = await world.sessions.get("s1")
    await world.sessions.update(row.model_copy(update={
        "parked_status": "parked", "parked_event_key": yielded.event_key, "status": SessionStatus.WAITING,
        "parked_state": {"yielded": {**yielded.to_jsonable(), "tool_name": CANVAS_WAIT_TOOL}},
    }))


async def _subscriptions(world: World) -> list[EventSubscription]:
    return list((await world.sp.get_storage(EventSubscription).list(OffsetPage(offset=0, length=50))).items)


async def _wait(provider, ctx, **arguments):
    """The Yielded a wait that parks returns (the provider raises it as YieldToWorker)."""
    with pytest.raises(YieldToWorker) as exc:
        await provider.call(tool_name="canvas_wait", arguments=arguments, ctx=ctx)
    return exc.value.yielded


# ---- a wait that is already over --------------------------------------------------------------------------------------------------------


async def test_an_unseen_matching_click_returns_at_once(provider, ctx, world):
    await _event(world, "ok", "click")
    result = await provider.call(tool_name="canvas_wait", arguments={"ids": ["ok"]}, ctx=ctx)
    body = json.loads(result.output)
    assert not result.is_error and [e["id"] for e in body["events"]] == ["e1"] and body["events"][0]["element"] == "ok"
    assert body["rev"] == 1 and body["values"] == {}
    assert (await world.canvas("s1")).seen_event_seq == 1, "the event it returned is marked seen"
    assert await _subscriptions(world) == [], "a wait that does not park leaves no subscription behind"


async def test_the_values_in_the_result_are_the_current_ones(provider, ctx, world):
    await _event(world, "genre", "change", value="Lo-fi")
    await _event(world, "ok", "click")
    body = json.loads((await provider.call(tool_name="canvas_wait", arguments={}, ctx=ctx)).output)
    assert [e["id"] for e in body["events"]] == ["e2"], "without kinds only an event that wakes ends the wait"
    assert body["values"] == {"genre": "Lo-fi"} and body["rev"] == 2


async def test_a_wait_for_a_kind_that_does_not_wake_takes_it(provider, ctx, world):
    await _event(world, "genre", "change", value="Lo-fi")
    body = json.loads((await provider.call(tool_name="canvas_wait", arguments={"kinds": ["change"]}, ctx=ctx)).output)
    assert [(e["id"], e["kind"], e["wake"]) for e in body["events"]] == [("e1", "change", False)]


async def test_a_wait_returns_every_unseen_matching_event_and_marks_them_all(provider, ctx, world):
    await _event(world, "ok", "click")
    await _event(world, "cancel", "click")
    await _event(world, "ok", "click")
    body = json.loads((await provider.call(tool_name="canvas_wait", arguments={"ids": ["ok"]}, ctx=ctx)).output)
    assert [e["id"] for e in body["events"]] == ["e1", "e3"]
    assert (await world.canvas("s1")).seen_event_seq == 3


async def test_events_of_other_elements_do_not_end_it(provider, ctx, world):
    await _event(world, "cancel", "click")
    y = await _wait(provider, ctx, ids=["ok"])
    assert y.tool_name == "canvas_wait"
    assert (await world.canvas("s1")).seen_event_seq == 0, "an event the wait did not take stays unread"


async def test_an_event_the_agent_has_read_does_not_end_it(provider, ctx, world):
    await _event(world, "ok", "click")
    await provider.call(tool_name="canvas_get", arguments={}, ctx=ctx)           # reads and marks e1
    await _wait(provider, ctx)


# ---- a wait that parks --------------------------------------------------------------------------------------------------------------


async def test_no_match_parks_on_a_one_shot_subscription(provider, ctx, world):
    y = await _wait(provider, ctx, ids=["ok"])
    assert (y.tool_name, y.event_key, y.timeout) == ("canvas_wait", "canvaswait:s1:call-1", 600)
    [sub] = await _subscriptions(world)
    assert sub.filter == canvas_wait_filter("s1", ids=["ok"], kinds=[]) and y.resume_metadata["subscription_id"] == sub.id
    assert sub.sink == SessionWakeSink(event_key="canvaswait:s1:call-1", session_id="s1", one_shot=True)
    assert sub.managed_by == "system"
    assert y.resume_metadata == {"subscription_id": sub.id, "ids": ["ok"], "kinds": [], "after_event_seq": 0}
    store = world.sp.get_event_store()
    assert await store.get_cursor(sub.id) == await store.max_id(), "the cursor is pinned at the head of the log"


async def test_the_wait_remembers_where_the_canvas_was(provider, ctx, world):
    await _event(world, "genre", "change", value="Lo-fi")
    await _event(world, "genre", "change", value="Jazz")
    y = await _wait(provider, ctx, kinds=["click"], timeout_s=30)
    assert y.timeout == 30 and y.resume_metadata["after_event_seq"] == 2 and y.resume_metadata["kinds"] == ["click"]


async def test_the_default_timeout_is_ten_minutes(provider, ctx):
    assert (await _wait(provider, ctx)).timeout == 600


@pytest.mark.parametrize("meta, event, expected", [
    ({"ids": [], "kinds": []}, {"element": "go", "kind": "click", "wake": True}, True),
    ({"ids": [], "kinds": []}, {"element": "genre", "kind": "change", "wake": False}, False),
    ({"ids": ["genre"], "kinds": ["change"]}, {"element": "genre", "kind": "change", "wake": False}, True),
    ({"ids": ["ok"], "kinds": []}, {"element": "cancel", "kind": "click", "wake": True}, False),
    ({"ids": ["ok"], "kinds": []}, {"element": "ok", "kind": "click", "wake": True}, True),
    ({"ids": [], "kinds": ["click"]}, {"element": "ok", "kind": "click", "wake": True}, True),
    ({"ids": [], "kinds": ["click"]}, {"element": "viz", "kind": "emit", "wake": True}, False),
    ({"ids": [], "kinds": ["emit", "click"]}, {"element": "viz", "kind": "emit", "wake": True}, True),
    ({"ids": ["ok"], "kinds": ["change"]}, {"element": "ok", "kind": "click", "wake": True}, False),
    ({"ids": [], "kinds": []}, {"element": "viz", "kind": "emit", "wake": True}, True),
    ({"ids": [], "kinds": []}, {"element": "player", "kind": "play", "wake": False}, False),
    ({"ids": ["a_b"], "kinds": []}, {"element": "a_b", "kind": "click", "wake": True}, True),
    ({"ids": ["a_b"], "kinds": []}, {"element": "axb", "kind": "click", "wake": True}, False),
])
def test_the_filter_and_the_predicate_agree(meta, event, expected):
    envelope = Event(id=1, event_type="canvas.event", occurred_at=NOW, session_id="s1", payload=event)
    assert canvas_wait_matches(meta, event) is expected
    assert matches(envelope, canvas_wait_filter("s1", ids=meta["ids"], kinds=meta["kinds"])) is expected


def test_the_filter_is_this_sessions_and_this_kind_of_event_only():
    other_session = Event(id=1, event_type="canvas.event", occurred_at=NOW, session_id="s2", payload={"element": "ok", "kind": "click", "wake": True})
    other_type = Event(id=1, event_type="canvas.updated", occurred_at=NOW, session_id="s1", payload={"element": "ok", "kind": "click", "wake": True})
    mine = Event(id=1, event_type="canvas.event", occurred_at=NOW, session_id="s1", payload={"element": "ok", "kind": "click", "wake": True})
    wanted = canvas_wait_filter("s1", ids=[], kinds=[])
    assert [matches(e, wanted) for e in (other_session, other_type, mine)] == [False, False, True]


@pytest.mark.parametrize("element, kind", [("ok2", "click_more"), ("dok", "more_click"), ("xokx", "xclickx")])
def test_the_alternations_are_anchored_so_a_longer_word_does_not_match(element, kind):
    longer = Event(id=1, event_type="canvas.event", occurred_at=NOW, session_id="s1", payload={"element": element, "kind": kind, "wake": True})
    assert not matches(longer, canvas_wait_filter("s1", ids=["ok"], kinds=[]))
    assert not matches(longer, canvas_wait_filter("s1", ids=[], kinds=["click"]))
    exact = Event(id=1, event_type="canvas.event", occurred_at=NOW, session_id="s1", payload={"element": "ok", "kind": "click", "wake": True})
    assert matches(exact, canvas_wait_filter("s1", ids=["ok", "cancel"], kinds=["emit", "click"]))


def test_the_keys():
    ctx = ToolContext(tool_call_id="call-1", session_id="s1", workspace_id="ws-1")
    assert CANVAS_WAIT_PREFIX == "canvaswait:" and CANVAS_WAIT_TOOL == "canvas_wait"
    assert canvas_wait_event_key(ctx, None) == "canvaswait:s1:call-1"
    assert canvas_wait_event_key(ctx, "worker[0]") == "canvaswait:s1:worker[0]:call-1"


async def test_a_graph_node_wait_has_a_node_scoped_key(provider, ctx, world):
    from primer.graph._node_identity import reset_current_graph_node_id, set_current_graph_node_id

    entered = set_current_graph_node_id("worker[0]")
    try:
        y = await _wait(provider, ctx)
    finally:
        reset_current_graph_node_id(entered)
    assert y.event_key == "canvaswait:s1:worker[0]:call-1"
    [sub] = await _subscriptions(world)
    assert sub.sink.event_key == "canvaswait:s1:worker[0]:call-1"


@pytest.mark.parametrize("timeout_s", [1, 1.5, 600, 86400])
async def test_timeouts_in_range_are_taken(provider, ctx, timeout_s):
    assert (await _wait(provider, ctx, timeout_s=timeout_s)).timeout == timeout_s


@pytest.mark.parametrize("timeout_s", [0, 0.5, 86401, -5, "soon"])
async def test_timeouts_out_of_range_are_refused_and_store_nothing(provider, ctx, world, timeout_s):
    result = await provider.call(tool_name="canvas_wait", arguments={"timeout_s": timeout_s}, ctx=ctx)
    assert result.is_error and json.loads(result.output)["type"] == "validation-error"
    assert await _subscriptions(world) == []


@pytest.mark.parametrize("arguments", [{"ids": ["Title"]}, {"ids": ["ok"] * 51}, {"kinds": ["hover"]}, {"kinds": "click"}])
async def test_bad_arguments_are_refused_and_store_nothing(provider, ctx, world, arguments):
    result = await provider.call(tool_name="canvas_wait", arguments=arguments, ctx=ctx)
    assert result.is_error and json.loads(result.output)["type"] == "validation-error"
    assert await _subscriptions(world) == []


async def test_without_a_session_the_wait_refuses(provider):
    unsupported = {"type": "unsupported-surface", "message": "the canvas belongs to a workspace session; this call has none"}
    for ctx in (None, ToolContext(tool_call_id="c", session_id=None, workspace_id=None, chat_id="chat-1")):
        result = await provider.call(tool_name="canvas_wait", arguments={}, ctx=ctx)
        assert result.is_error and json.loads(result.output) == unsupported


async def test_the_kinds_are_the_kinds_a_canvas_sends(provider):
    schema = {t.id: t async for t in provider.list_tools()}["canvas_wait"].args_schema
    assert schema["properties"]["kinds"]["anyOf"][0]["items"]["enum"] == list(CANVAS_EVENT_KINDS)
    assert schema["properties"]["timeout_s"]["minimum"] == 1 and schema["properties"]["timeout_s"]["maximum"] == 86400
    assert schema["properties"]["ids"]["anyOf"][0]["maxItems"] == 50


# ---- the cursor is pinned first (the lead's ruling on #790) ------------------------------------------------------------------------------


async def test_the_cursor_is_pinned_before_the_canvas_is_checked(provider, ctx, world):
    order: list[str] = []
    store = world.sp.get_event_store()
    real_set_cursor, real_snapshot = store.set_cursor, provider.service.snapshot

    async def set_cursor(*args, **kwargs):
        order.append("pin")
        return await real_set_cursor(*args, **kwargs)

    async def snapshot(*args, **kwargs):
        order.append("check")
        return await real_snapshot(*args, **kwargs)

    store.set_cursor, provider.service.snapshot = set_cursor, snapshot
    await _wait(provider, ctx, ids=["ok"])
    assert order == ["pin", "check"]


async def test_a_click_between_the_check_and_the_park_is_not_missed(provider, ctx, world):
    """The check finds nothing; a click lands before the tool returns. With the cursor pinned AFTER the check that click would sit in the
    log before the cursor, and neither the check nor the subscription would ever see it."""
    real_snapshot = provider.service.snapshot

    async def check_then_click(*args, **kwargs):
        row = await real_snapshot(*args, **kwargs)
        await _event(world, "ok", "click")
        return row

    provider.service.snapshot = check_then_click
    y = await _wait(provider, ctx, ids=["ok"])
    await _park(world, y)
    bus = world.bus
    assert await EventDispatcher(storage_provider=world.sp, event_bus=bus).drain_once() == 1
    [(key, envelope)] = [(k, p) for k, p in bus.published if k == y.event_key]
    assert envelope["payload"]["id"] == "e1" and envelope["payload"]["element"] == "ok"


async def test_a_click_between_the_pin_and_the_check_ends_the_wait_at_once(provider, ctx, world):
    store = world.sp.get_event_store()
    real_set_cursor = store.set_cursor

    async def pin_then_click(*args, **kwargs):
        await real_set_cursor(*args, **kwargs)
        await _event(world, "ok", "click")

    store.set_cursor = pin_then_click
    body = json.loads((await provider.call(tool_name="canvas_wait", arguments={"ids": ["ok"]}, ctx=ctx)).output)
    assert [e["id"] for e in body["events"]] == ["e1"]
    assert await _subscriptions(world) == [], "the subscription of a wait that did not park is gone"


async def test_a_check_that_fails_leaves_no_subscription(provider, ctx, world):
    async def broken(*args, **kwargs):
        raise RuntimeError("storage down")

    provider.service.snapshot = broken
    with pytest.raises(RuntimeError):
        await provider.call(tool_name="canvas_wait", arguments={}, ctx=ctx)
    assert await _subscriptions(world) == []


# ---- the wake ---------------------------------------------------------------------------------------------------------------------


async def test_the_dispatcher_wakes_the_park_for_a_matching_click_only(provider, ctx, world):
    y = await _wait(provider, ctx, ids=["ok"])
    await _park(world, y)
    dispatcher = EventDispatcher(storage_provider=world.sp, event_bus=world.bus)
    await _event(world, "cancel", "click")
    await _event(world, "genre", "change", value="Lo-fi")
    assert await dispatcher.drain_once() == 0 and [k for k, _ in world.bus.published if k == y.event_key] == []
    await _event(world, "ok", "click")
    assert await dispatcher.drain_once() == 1
    [(key, envelope)] = [(k, p) for k, p in world.bus.published if k == y.event_key]
    assert (envelope["event_type"], envelope["session_id"], envelope["payload"]["id"]) == ("canvas.event", "s1", "e3")
    assert await _subscriptions(world) == [], "the subscription was one-shot"


async def test_the_full_loop_the_click_is_the_tool_result(provider, ctx, world):
    y = await _wait(provider, ctx, ids=["ok"])
    await _park(world, y)
    await _event(world, "genre", "change", value="Lo-fi")
    await _event(world, "ok", "click")
    await EventDispatcher(storage_provider=world.sp, event_bus=world.bus).drain_once()
    [(_, envelope)] = [(k, p) for k, p in world.bus.published if k == y.event_key]
    result = await get_resume_hook("canvas_wait")(y.resume_metadata, envelope, resume_context(provider))
    body = json.loads(result.output)
    assert not result.is_error and [e["id"] for e in body["events"]] == ["e2"] and body["events"][0]["kind"] == "click"
    assert body["values"] == {"genre": "Lo-fi"} and body["rev"] == 2
    assert (await world.canvas("s1")).seen_event_seq == 2


# ---- the resume hook ---------------------------------------------------------------------------------------------------------------


def test_the_resume_hook_is_registered_at_import():
    assert get_resume_hook("canvas_wait") is canvas_wait_resume


async def _envelope(world: World, element: str = "ok", kind: str = "click", **kwargs: Any) -> dict[str, Any]:
    event = await _event(world, element, kind, **kwargs)
    return {"id": 99, "event_type": "canvas.event", "session_id": "s1", "workspace_id": "ws-1", "payload": event.public()}


async def test_the_resume_returns_the_event_and_current_values(provider, world):
    await _event(world, "genre", "change", value="Lo-fi")
    envelope = await _envelope(world)
    result = await canvas_wait_resume({"ids": ["ok"]}, envelope, resume_context(provider))
    body = json.loads(result.output)
    assert not result.is_error and body == {"events": [envelope["payload"]], "rev": 2, "values": {"genre": "Lo-fi"}}
    assert (await world.canvas("s1")).seen_event_seq == 2, "the event that ended the wait is marked seen"


async def test_the_resume_without_a_registry_is_the_event_alone(provider, world):
    envelope = await _envelope(world)
    result = await canvas_wait_resume({}, envelope, resume_context(provider, resolve=False))
    assert json.loads(result.output) == {"events": [envelope["payload"]]}
    assert (await world.canvas("s1")).seen_event_seq == 0, "without the canvas there is nothing to mark"


async def test_the_resume_without_a_session_is_the_event_alone(provider, world):
    envelope = await _envelope(world)
    result = await canvas_wait_resume({}, envelope, resume_context(provider, session_id=None))
    assert json.loads(result.output) == {"events": [envelope["payload"]]}


async def test_the_resume_survives_a_registry_that_cannot_answer(world):
    envelope = await _envelope(world)

    async def broken(toolset_id: str):
        raise RuntimeError("registry down")

    result = await canvas_wait_resume({}, envelope, ResumeContext("canvas_wait", "call-1", "s1", broken))
    assert not result.is_error and json.loads(result.output) == {"events": [envelope["payload"]]}


async def test_the_resume_survives_a_canvas_that_cannot_be_read(provider, world):
    envelope = await _envelope(world)

    async def broken(session_id):
        raise RuntimeError("storage down")

    provider.service.current = broken
    result = await canvas_wait_resume({}, envelope, resume_context(provider))
    assert not result.is_error and json.loads(result.output) == {"events": [envelope["payload"]]}


async def test_a_timeout_reports_timeout_true(provider, world):
    await _event(world, "genre", "change", value="Lo-fi")
    result = await canvas_wait_resume({"ids": ["ok"]}, YieldTimeout(elapsed_seconds=600.0), resume_context(provider))
    assert not result.is_error and json.loads(result.output) == {"timeout": True, "rev": 2, "values": {"genre": "Lo-fi"}}
    assert (await world.canvas("s1")).seen_event_seq == 0, "a timeout marks nothing"
    bare = await canvas_wait_resume({}, YieldTimeout(elapsed_seconds=1.0), resume_context(provider, resolve=False))
    assert json.loads(bare.output) == {"timeout": True}


async def test_a_timeout_or_a_cancel_removes_the_subscription_nothing_will_deliver_to(provider, ctx, world):
    store = world.sp.get_event_store()
    for payload in (YieldTimeout(elapsed_seconds=600.0), YieldCancelled(reason="skipped", cancelled_at=NOW, elapsed_seconds=1.0)):
        y = await _wait(provider, ctx, ids=["ok"])
        [sub] = await _subscriptions(world)
        assert await store.get_cursor(sub.id) is not None
        result = await canvas_wait_resume(y.resume_metadata, payload, resume_context(provider))
        assert not result.is_error
        assert await _subscriptions(world) == [] and await store.get_cursor(sub.id) is None
    bare = await _wait(provider, ctx)
    await canvas_wait_resume(bare.resume_metadata, YieldTimeout(elapsed_seconds=1.0), resume_context(provider, resolve=False))
    assert len(await _subscriptions(world)) == 1, "without the registry there is no storage to clean; the dispatcher collects it later"


async def test_a_timeout_whose_subscription_is_already_gone_is_still_a_timeout(provider, ctx, world):
    y = await _wait(provider, ctx)
    await world.sp.get_storage(EventSubscription).delete(y.resume_metadata["subscription_id"])
    result = await canvas_wait_resume(y.resume_metadata, YieldTimeout(elapsed_seconds=1.0), resume_context(provider))
    assert json.loads(result.output)["timeout"] is True


async def test_a_timeout_on_a_session_without_a_canvas(world):
    other = make_world()
    await seed_session(other)
    result = await canvas_wait_resume({}, YieldTimeout(elapsed_seconds=1.0), resume_context(build_canvas_toolset(storage_provider=other.sp)))
    assert json.loads(result.output) == {"timeout": True, "rev": 0, "values": {}}


async def test_a_cancel_reports_the_reason(provider, world):
    result = await canvas_wait_resume({}, YieldCancelled(reason="operator skipped it", cancelled_at=NOW, elapsed_seconds=3.0), resume_context(provider))
    assert not result.is_error and json.loads(result.output) == {"cancelled": True, "cancel_reason": "operator skipped it"}
    none = await canvas_wait_resume({}, YieldCancelled(reason=None, cancelled_at=NOW, elapsed_seconds=3.0), resume_context(provider))
    assert json.loads(none.output) == {"cancelled": True, "cancel_reason": None}


# ---- a click that is both the result and a queued steer: delivered once (Review Focus 1) -------------------------------------------------


async def _queue(world: World, session_id: str, event_id: str | None, *, suffix: str = "", attribution: Any = "canvas") -> PendingSessionMessage:
    shape = {"canvas_event": {"id": event_id, "element": "ok"}} if attribution == "canvas" else attribution
    row = PendingSessionMessage(
        id=f"{session_id}:pending:{NOW.isoformat()}:{event_id}{suffix}", session_id=session_id,
        parts=[{"type": "text", "text": f"<canvas_event id={event_id}>"}], attribution=shape, enqueued_at=NOW, created_at=NOW,
    )
    return await world.sp.get_storage(PendingSessionMessage).create(row)


async def test_the_resume_drops_a_queued_steer_for_the_same_event(provider, world):   # Review Focus 1
    await seed_session(world, "s2", "ws-1")
    await _queue(world, "s1", "e3")
    await _queue(world, "s1", "e4")
    await _queue(world, "s2", "e3")                         # another session's queue is not this wait's business
    await _queue(world, "s1", None, suffix="plain", attribution=None)
    await _queue(world, "s1", None, suffix="peer", attribution={"peer": {"from_session_id": "x"}})
    for element in ("ok", "ok", "ok"):
        await _event(world, element, "click")
    envelope = {"id": 9, "event_type": "canvas.event", "session_id": "s1", "payload": (await world.canvas("s1")).events[2].public()}
    assert envelope["payload"]["id"] == "e3"
    await canvas_wait_resume({"ids": ["ok"]}, envelope, resume_context(provider))
    kept = [row.id for row in (await world.sp.get_storage(PendingSessionMessage).list(OffsetPage(offset=0, length=50))).items]
    assert sorted(kept) == sorted([
        f"s1:pending:{NOW.isoformat()}:e4", f"s2:pending:{NOW.isoformat()}:e3", f"s1:pending:{NOW.isoformat()}:Noneplain",
        f"s1:pending:{NOW.isoformat()}:Nonepeer",
    ])


async def test_a_wait_that_returns_at_once_drops_the_queued_copy_too(provider, ctx, world):
    """The click came while the turn was running and was queued as a steer; the turn then asks to wait: it is told now and not again later."""
    event = await _event(world, "ok", "click")
    await _queue(world, "s1", event.id)
    await _queue(world, "s1", "e9")
    body = json.loads((await provider.call(tool_name="canvas_wait", arguments={"ids": ["ok"]}, ctx=ctx)).output)
    assert [e["id"] for e in body["events"]] == [event.id]
    rows = (await world.sp.get_storage(PendingSessionMessage).list(OffsetPage(offset=0, length=50))).items
    assert [row.attribution["canvas_event"]["id"] for row in rows] == ["e9"]


async def test_a_queue_that_cannot_be_read_does_not_fail_the_wait(provider, ctx, world, caplog):
    await _event(world, "ok", "click")

    async def down(*args, **kwargs):
        raise RuntimeError("queue down")

    world.sp.get_storage(PendingSessionMessage).find = down
    with caplog.at_level("WARNING"):
        result = await provider.call(tool_name="canvas_wait", arguments={}, ctx=ctx)
    assert not result.is_error and [e["id"] for e in json.loads(result.output)["events"]] == ["e1"]
    assert (await world.canvas("s1")).seen_event_seq == 1 and "could not be dropped" in caplog.text


async def test_events_that_cannot_be_marked_seen_do_not_fail_the_wait(provider, ctx, world, caplog):
    await _event(world, "ok", "click")

    async def down(session_id, through_seq):
        raise RuntimeError("canvas storage down")

    provider.service.mark_seen = down
    with caplog.at_level("WARNING"):
        result = await provider.call(tool_name="canvas_wait", arguments={}, ctx=ctx)
    assert not result.is_error and [e["id"] for e in json.loads(result.output)["events"]] == ["e1"]
    assert "could not be marked seen" in caplog.text


async def test_the_steers_are_dropped_before_the_events_are_marked_seen(provider, ctx, world):
    """A crash between the two leaves the events unread (shown again), never a click delivered twice."""
    event = await _event(world, "ok", "click")
    await _queue(world, "s1", event.id)
    order: list[str] = []
    steers, real_mark = world.sp.get_storage(PendingSessionMessage), provider.service.mark_seen
    real_delete = steers.delete

    async def delete(row_id, **kwargs):
        order.append("drop")
        return await real_delete(row_id, **kwargs)

    async def mark(session_id, through_seq):
        order.append("mark")
        return await real_mark(session_id, through_seq)

    steers.delete, provider.service.mark_seen = delete, mark
    await provider.call(tool_name="canvas_wait", arguments={}, ctx=ctx)
    assert order == ["drop", "mark"]


async def test_drop_queued_steers_reads_every_page_and_skips_a_row_already_gone(world):
    for i in range(205):
        await _queue(world, "s1", f"e{i}")
    store = world.sp.get_storage(PendingSessionMessage)
    real_delete = store.delete
    deleted: list[str] = []

    async def racing_delete(row_id, **kwargs):
        if row_id.endswith(":e0"):
            await real_delete(row_id)            # another process realized it first
        deleted.append(row_id)
        return await real_delete(row_id, **kwargs)

    store.delete = racing_delete
    dropped = await drop_queued_steers(world.sp, "s1", ["e0", "e204", "e100", "nope"])
    assert dropped == 2 and len(deleted) == 3
    assert await drop_queued_steers(world.sp, "s1", []) == 0


async def test_nothing_is_read_when_there_is_nothing_to_drop(world):
    async def must_not_read(*args, **kwargs):
        raise AssertionError("the queue was read")

    world.sp.get_storage(PendingSessionMessage).find = must_not_read
    assert await drop_queued_steers(world.sp, "s1", []) == 0
    assert await drop_queued_steers(world.sp, "s1", iter(())) == 0


async def test_events_without_a_seq_or_an_id_mark_and_drop_nothing(provider, world):
    marked: list[tuple[str, int]] = []
    dropped: list[Any] = []

    async def mark_seen(session_id, through_seq):
        marked.append((session_id, through_seq))

    async def find(*args, **kwargs):
        dropped.append(args)
        raise AssertionError("no event id, so no queue to read")

    provider.service.mark_seen = mark_seen
    world.sp.get_storage(PendingSessionMessage).find = find
    await consume_events(provider.service, world.sp, "s1", [{"kind": "click"}, {"id": 5, "seq": True}])
    assert marked == [] and dropped == []
    await consume_events(provider.service, world.sp, "s1", [{"seq": 4}, {"seq": 9}, {"seq": 2}])
    assert marked == [("s1", 9)], "the highest seq, once"


async def test_a_wake_that_carries_no_event_is_an_empty_result(provider, world):
    for payload in ({}, {"payload": "not an object"}, None, "text"):
        result = await canvas_wait_resume({}, payload, resume_context(provider))
        assert not result.is_error and json.loads(result.output) == {"events": [], "rev": 1, "values": {}}
    assert (await world.canvas("s1")).seen_event_seq == 0


# ---- how the tool is declared -----------------------------------------------------------------------------------------------------


async def test_the_tool_is_declared_as_the_plan_says(provider):
    tools = {t.id: t async for t in provider.list_tools()}
    wait = tools["canvas_wait"]
    assert (wait.yields, wait.requires_workspace, wait.required_role, wait.interruptible) == (True, True, "user", True)
    assert provider.is_yielding("canvas_wait") and provider.requires_workspace("canvas_wait") and provider.required_role("canvas_wait") == "user"
    assert wait.description.startswith("Pause this turn until the user acts on the canvas; returns the matching events with the current values, or {timeout: true}.")
    assert "without kinds it waits for an event that wakes (a Button click or an Html emit)" in wait.description
    assert "It returns at once if such an event arrived since your last canvas read. timeout_s defaults to 600 (at most 86400)." in wait.description
    assert 'Example: {"ids":["ok","cancel"]} -> ``{events: [{id, element, kind, ...}], values, rev}``  (yielding; worker released)' in wait.description
    assert [name for name in tools] == ["canvas_get", "canvas_apply", "canvas_wait"]


async def test_the_wait_is_not_exposable_over_mcp(provider):
    from primer.mcp.safety import is_exposable

    wait = {t.id: t async for t in provider.list_tools()}["canvas_wait"]
    assert is_exposable(wait, provider=provider) == (False, "yielding_unsupported")


def test_the_docs_registry_sees_all_three_canvas_tools():            # pivot P4's requirement
    # bare ids: the docs registry keys a toolset's tools by them (and builds itself with asyncio.run, so this test is not async);
    # the scoped ids are the catalog entries' (next test)
    assert {"canvas_get", "canvas_apply", "canvas_wait"} <= set(built_in_tools()["canvas"])


async def test_the_tool_catalog_lists_all_three_and_marks_only_the_wait_as_pausing():
    entries = {e.scoped_id: e for e in await collect_tool_entries(built_in_providers()) if e.toolset_id == "canvas"}
    assert set(entries) == {"canvas__canvas_get", "canvas__canvas_apply", "canvas__canvas_wait"}
    assert entries["canvas__canvas_wait"].pauses_turn is True and entries["canvas__canvas_get"].pauses_turn is False
    assert entries["canvas__canvas_apply"].pauses_turn is False
