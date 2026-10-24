"""Delivering a canvas event by the state of its session (pivot P5, Task 12; plan Decision 7, Review Focus 1).

A waking event (a Button click, an Html emit) on an idle session wakes it with the ``<canvas_event>`` block as its next instruction; on a
session with a turn open, or parked on anything but a ``canvas_wait`` the event matches (an ``ask_user``, an approval, a wait on other
elements), it is queued as a steer and the park is untouched: a click never answers a gate. A parked ``canvas_wait`` it matches takes it
(the event dispatcher resumes the wait). A silent event (a Select change) is only recorded, and reaches the agent in the next digest.

What the user entered (a value, an emit's data) is in the block's TEXT only: the queued steer's attribution, the USER_INPUT's
``payload.canvas_event`` and every ``canvas_event`` record carry the event without its ``value`` and ``data`` (the lead's ruling 1 on S5).

The world is the canvas suite's in-memory one (``tests/canvas/_fakes.py``): the real ``CanvasService``, ``wake_session``,
``store_pending_steer`` and record writer, over the fake storage, workspace and bus; the scheduler and the claim engine note their calls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from primer.canvas.delivery import (
    CANVAS_EVENT_PAYLOAD_KEY,
    DELIVERY_QUEUED,
    DELIVERY_RECORDED,
    DELIVERY_WAIT,
    DELIVERY_WOKEN,
    deliver_canvas_event,
    event_attribution,
)
from primer.canvas.lang import parse_program
from primer.int.claim import ClaimKind
from primer.model.canvas import CanvasEvent
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.workspace_session import PendingSessionMessage, SessionStatus
from primer.session.enqueue import SessionWakeDeps
from primer.session.pending_messages import realize_next_pending
from tests.canvas._fakes import World, make_world, seed_session

PROGRAM = """
root = Column([controls, viz])
controls = Row([genre, play])
genre = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")
play = Button("Play", "primary")
viz = Html("<div id=b>pick one</div>", 60)
"""
#: What the user entered, distinctive enough to find anywhere it leaked.
SECRET_NOTE = "the user's private note"
INPUT = ("value", "data")


class Scheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, session_id: str) -> None:
        self.enqueued.append(session_id)


class Engine:
    def __init__(self) -> None:
        self.upserts: list[tuple[Any, str]] = []

    async def upsert(self, kind: Any, entity_id: str, **_: Any) -> None:
        self.upserts.append((kind, entity_id))


def _park(tool_name: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    key = f"{'canvaswait' if tool_name == 'canvas_wait' else tool_name}:s1:c1"
    return {
        "parked_status": "parked",
        "parked_event_key": key,
        "parked_at": datetime.now(UTC),
        "parked_state": {
            "tool_call_id": "c1",
            "yielded": {"tool_name": tool_name, "event_key": key, "resume_metadata": dict(meta or {})},
        },
    }


#: The session states of the table: the fields seeded on the row.
ROW_STATES: dict[str, dict[str, Any]] = {
    "idle": {"turn_status": "idle", "last_seq": 4},
    "running": {"turn_status": "running", "status": SessionStatus.RUNNING, "last_seq": 4},
    "parked-ask-user": {"turn_status": "idle", "last_seq": 4, **_park("ask_user", {"question": "which one?"})},
    "parked-approval": {"turn_status": "idle", "last_seq": 4, **_park("_approval", {"original_call": {"name": "x"}})},
    "wait-match": {"turn_status": "idle", "last_seq": 4, **_park("canvas_wait", {"ids": ["play"], "kinds": []})},
    "wait-other-ids": {"turn_status": "idle", "last_seq": 4, **_park("canvas_wait", {"ids": ["other"], "kinds": []})},
    "paused": {"turn_status": "idle", "status": SessionStatus.PAUSED, "last_seq": 4},
}
#: The events of the table.
EVENTS: dict[str, dict[str, Any]] = {
    "click play": {"element": "play", "kind": "click"},
    "change genre": {"element": "genre", "kind": "change", "value": "Lo-fi"},
    "emit viz": {"element": "viz", "kind": "emit", "name": "pick", "data": {"note": SECRET_NOTE}},
}


async def _world(row_state: str = "idle") -> World:
    w = make_world(script_check=lambda body: [])
    await seed_session(w, **ROW_STATES[row_state])
    await w.service.apply(
        session_id="s1", workspace_id="ws-1", statements=parse_program(PROGRAM), unit="line", base_rev=None, source="agent", by="agent",
    )
    return w


async def _record(w: World, event: str, by: str = "alice") -> CanvasEvent:
    return await w.service.record_event(session_id="s1", workspace_id="ws-1", by=by, **EVENTS[event])


async def _deliver(w: World, event: CanvasEvent, **parts: Any) -> str:
    row = await w.canvas("s1")
    return await deliver_canvas_event(
        event=event, state=w.service.state_of(row), session_id="s1", workspace_id="ws-1", storage_provider=w.sp,
        scheduler=parts.get("scheduler") or Scheduler(), claim_engine=parts.get("engine") or Engine(), workspace_registry=w.registry,
        event_bus=w.bus,
    )


def _pending(w: World) -> list[PendingSessionMessage]:
    return sorted(w.sp.get_storage(PendingSessionMessage)._data.values(), key=lambda r: r.enqueued_at)


def _keys(blob: Any) -> set[str]:
    """Every key of every mapping anywhere in ``blob``."""
    if isinstance(blob, dict):
        return set(blob) | {k for v in blob.values() for k in _keys(v)}
    if isinstance(blob, (list, tuple)):
        return {k for v in blob for k in _keys(v)}
    return set()


# ---- the table -----------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("row_state", "event", "outcome"), [
    ("idle", "click play", DELIVERY_WOKEN),
    ("running", "click play", DELIVERY_QUEUED),
    ("parked-ask-user", "click play", DELIVERY_QUEUED),
    ("parked-approval", "click play", DELIVERY_QUEUED),
    ("wait-match", "click play", DELIVERY_WAIT),
    ("wait-other-ids", "click play", DELIVERY_QUEUED),
    ("idle", "change genre", DELIVERY_RECORDED),
    ("running", "change genre", DELIVERY_RECORDED),
    ("paused", "click play", DELIVERY_WOKEN),
])
async def test_delivery_table(row_state: str, event: str, outcome: str) -> None:
    w = await _world(row_state)
    before = await w.sessions.get("s1")
    scheduler, engine = Scheduler(), Engine()
    ev = await _record(w, event)

    assert await _deliver(w, ev, scheduler=scheduler, engine=engine) == outcome

    row = await w.sessions.get("s1")
    user_inputs = w.records("s1", "user_input")
    records = w.records("s1", "canvas_event")
    if outcome == DELIVERY_WOKEN:
        [user_input] = user_inputs
        assert user_input["payload"][CANVAS_EVENT_PAYLOAD_KEY]["id"] == ev.id == "e1"
        assert user_input["payload"]["text"].startswith('<canvas_event id="e1" ')
        [(text, _parts)] = w.registry.workspace("ws-1").slots["s1"].appended
        assert text.startswith("<canvas_event") and text == user_input["payload"]["text"]
        assert row.turn_status == "claimable" and row.status == SessionStatus.RUNNING and not row.pause_requested
        assert scheduler.enqueued == ["s1"] and engine.upserts == [(ClaimKind.SESSION, "s1")]
        assert records == [] and _pending(w) == []
    elif outcome == DELIVERY_QUEUED:
        [queued] = _pending(w)
        assert queued.attribution == {CANVAS_EVENT_PAYLOAD_KEY: event_attribution(ev)}
        assert queued.parts == [{"type": "text", "text": queued.parts[0]["text"]}]
        assert queued.parts[0]["text"].startswith('<canvas_event id="e1" ')
        assert user_inputs == [] and records == [] and scheduler.enqueued == []
        assert (row.parked_status, row.parked_state, row.turn_status) == (before.parked_status, before.parked_state, before.turn_status)
    elif outcome == DELIVERY_WAIT:
        [record] = records
        assert record["payload"]["id"] == "e1" and record["seq"] == before.last_seq + 1
        assert _pending(w) == [] and user_inputs == [] and scheduler.enqueued == []
        assert (row.parked_status, row.parked_state) == (before.parked_status, before.parked_state)
    else:
        assert _pending(w) == [] and user_inputs == [] and scheduler.enqueued == []
        if row_state == "idle":
            [record] = records
            assert record["payload"]["id"] == "e1" and record["seq"] == before.last_seq + 1
        else:
            assert records == [], "while a turn runs its writer owns the seqs: the record is skipped"


async def test_a_queued_click_never_answers_the_gate_it_waits_behind() -> None:
    """Review Focus 1: the park of an ask_user stays exactly as it was, and nothing is published for its key."""
    w = await _world("parked-ask-user")
    before = await w.sessions.get("s1")
    assert await _deliver(w, await _record(w, "click play")) == DELIVERY_QUEUED
    row = await w.sessions.get("s1")
    assert row.model_dump() == {**before.model_dump(), "last_seq": row.last_seq}
    assert all("ask_user" not in key for key, _ in w.bus.published)


async def test_a_click_never_overtakes_a_queued_message() -> None:
    """An idle session with an older message still queued (audit G7-03 (b)): the click goes behind it, with its event, and stays unread."""
    from primer.session.pending_messages import store_pending_steer

    w = await _world("idle")
    await store_pending_steer(storage_provider=w.sp, session=await w.sessions.get("s1"), text="typed first", workspace_registry=w.registry)
    ev = await _record(w, "click play")

    assert await _deliver(w, ev) == DELIVERY_QUEUED

    first, click = _pending(w)
    assert first.parts[0]["text"] == "typed first" and click.attribution == {CANVAS_EVENT_PAYLOAD_KEY: event_attribution(ev)}
    assert w.records("s1", "user_input") == [] and [e.id for e in (await w.canvas("s1")).unseen_events()] == ["e1"]


async def test_a_wait_on_kinds_takes_a_silent_event() -> None:
    w = await _world("idle")
    await w.sessions.update((await w.sessions.get("s1")).model_copy(
        update=_park("canvas_wait", {"ids": ["genre"], "kinds": ["change"]}),
    ))
    assert await _deliver(w, await _record(w, "change genre")) == DELIVERY_WAIT


async def test_a_wait_only_takes_an_event_while_it_is_parked() -> None:
    """A wait being resumed (``resumable``) has already been answered: a second click is queued behind its turn, not swallowed."""
    w = await _world("wait-match")
    await w.sessions.update((await w.sessions.get("s1")).model_copy(update={"parked_status": "resumable", "turn_status": "claimable"}))
    assert await _deliver(w, await _record(w, "click play")) == DELIVERY_QUEUED


async def test_a_graph_nodes_canvas_wait_takes_the_event() -> None:
    w = await _world("idle")
    checkpoint = {"pending_toolcalls": [], "pending_agent_yields": [{
        "tool_name": "canvas_wait", "node_id": "n1", "tool_call_id": "c1", "event_key": "canvaswait:s1:n1:c1",
        "resume_metadata": {"ids": [], "kinds": []},
    }]}
    await w.sessions.update((await w.sessions.get("s1")).model_copy(update={
        "parked_status": "parked", "parked_at": datetime.now(UTC), "parked_state": {"graph_checkpoint": checkpoint},
    }))
    assert await _deliver(w, await _record(w, "click play")) == DELIVERY_WAIT


async def test_an_ended_session_is_recorded_and_nothing_else() -> None:
    w = await _world("idle")
    ev = await _record(w, "click play")
    await w.sessions.update((await w.sessions.get("s1")).model_copy(update={"status": SessionStatus.ENDED, "ended_reason": "cancelled"}))
    assert await _deliver(w, ev) == DELIVERY_RECORDED
    assert [r for r in w.records("s1") if r["kind"] != "canvas_op"] == [] and _pending(w) == []
    assert w.registry.workspace("ws-1").slots == {}


async def test_a_missing_session_is_recorded() -> None:
    w = await _world("idle")
    ev = await _record(w, "click play")
    await w.sessions.delete("s1")
    assert await _deliver(w, ev) == DELIVERY_RECORDED


@pytest.mark.parametrize("error", [NotFoundError("the workspace is gone"), ConflictError("cannot restart")], ids=["not found", "conflict"])
async def test_a_wake_the_session_refuses_is_recorded(monkeypatch, error: Exception) -> None:
    import primer.canvas.delivery as delivery

    async def refuse(**_: Any) -> None:
        raise error

    w = await _world("idle")
    ev = await _record(w, "click play")
    monkeypatch.setattr(delivery, "wake_session", refuse)
    assert await _deliver(w, ev) == DELIVERY_RECORDED
    assert _pending(w) == []


# ---- the input stays in the session's own input text (the lead's ruling 1 on S5) ------------------------------------------------------


def test_the_attribution_names_the_event_without_its_input() -> None:
    ev = CanvasEvent(
        id="e7", seq=7, rev=3, element="genre", type="Select", kind="change", source="control", by="alice", wake=True,
        at=datetime.now(UTC), value="Lo-fi", label="Genre",
    )
    assert event_attribution(ev) == {
        "id": "e7", "seq": 7, "element": "genre", "type": "Select", "kind": "change", "label": "Genre", "by": "alice",
    }
    emit = ev.model_copy(update={"element": "viz", "type": "Html", "kind": "emit", "label": None, "value": None, "name": "pick",
                                 "data": {"note": SECRET_NOTE}})
    assert event_attribution(emit) == {
        "id": "e7", "seq": 7, "element": "viz", "type": "Html", "kind": "emit", "label": None, "by": "alice", "name": "pick",
    }
    audio = ev.model_copy(update={"element": "player", "type": "Audio", "kind": "pause", "value": None, "position": 12.5})
    assert event_attribution(audio)["position"] == 12.5


@pytest.mark.parametrize(("row_state", "event", "outcome"), [
    ("idle", "emit viz", DELIVERY_WOKEN),
    ("running", "emit viz", DELIVERY_QUEUED),
    ("idle", "change genre", DELIVERY_RECORDED),
    ("wait-match", "click play", DELIVERY_WAIT),
])
async def test_every_structured_copy_carries_the_event_without_its_input(row_state: str, event: str, outcome: str) -> None:
    w = await _world(row_state)
    if event == "click play":
        await _record(w, "change genre")             # a user value, which the block's values line carries
    ev = await _record(w, event)
    assert await _deliver(w, ev) == outcome

    structured = [
        *[r.attribution for r in _pending(w)],
        *[{k: v for k, v in r["payload"].items() if k != "text"} for r in w.records("s1") if r["kind"] != "canvas_op"],
        *[payload for _key, payload in w.bus.published],
    ]
    assert not _keys(structured) & set(INPUT), structured
    entered = SECRET_NOTE if event == "emit viz" else "Lo-fi"
    assert entered not in json.dumps(structured, default=str)

    texts = [r.parts[0]["text"] for r in _pending(w)] + [r["payload"]["text"] for r in w.records("s1", "user_input")]
    if outcome in (DELIVERY_WOKEN, DELIVERY_QUEUED):
        [text] = texts
        assert entered in text, "the session's own input text carries what the user entered"


# ---- a queued click, realized ---------------------------------------------------------------------------------------------------------


def _wake_deps(w: World) -> SessionWakeDeps:
    return SessionWakeDeps(storage_provider=w.sp, scheduler=Scheduler(), claim_engine=Engine(), workspace_registry=w.registry,
                           event_bus=w.bus)


async def _turn_ends(w: World) -> None:
    await w.sessions.update((await w.sessions.get("s1")).model_copy(update={"turn_status": "idle"}))


async def test_a_realized_queued_click_keeps_its_canvas_payload() -> None:
    w = await _world("running")
    ev = await _record(w, "click play")
    assert await _deliver(w, ev) == DELIVERY_QUEUED
    [queued] = _pending(w)
    await _turn_ends(w)

    assert await realize_next_pending(storage_provider=w.sp, workspace_id="ws-1", session_id="s1", wake_deps=_wake_deps(w)) is True

    [user_input] = w.records("s1", "user_input")
    assert user_input["payload"][CANVAS_EVENT_PAYLOAD_KEY] == event_attribution(ev) == queued.attribution[CANVAS_EVENT_PAYLOAD_KEY]
    assert user_input["payload"]["text"] == queued.parts[0]["text"]
    assert _pending(w) == []


async def test_a_realized_queued_click_is_marked_read_and_not_before() -> None:
    """The lead's ruling on #795: the click is marked read when it is delivered, so canvas_get and canvas_wait do not return it again;
    while it waits in the queue it is unread (canvas_get lists it)."""
    w = await _world("running")
    assert await _deliver(w, await _record(w, "click play")) == DELIVERY_QUEUED
    assert [e.id for e in (await w.canvas("s1")).unseen_events()] == ["e1"]
    await _turn_ends(w)

    await realize_next_pending(storage_provider=w.sp, workspace_id="ws-1", session_id="s1", wake_deps=_wake_deps(w))

    assert (await w.canvas("s1")).unseen_events() == []
    [(text, parts)] = w.registry.workspace("ws-1").slots["s1"].appended
    assert text.startswith('<canvas_event id="e1" ') and parts == [], "marked as the delivered click, not repeated in a digest"


async def test_two_queued_clicks_arrive_once_each_in_order() -> None:
    """Review Focus 1, delivered once: the first click's wake does not list the second, still queued, in its digest."""
    w = await _world("running")
    first, second = await _record(w, "click play"), await _record(w, "emit viz")
    assert [await _deliver(w, first), await _deliver(w, second)] == [DELIVERY_QUEUED, DELIVERY_QUEUED]
    await _turn_ends(w)

    await realize_next_pending(storage_provider=w.sp, workspace_id="ws-1", session_id="s1", wake_deps=_wake_deps(w))
    [(text, parts)] = w.registry.workspace("ws-1").slots["s1"].appended
    assert text.startswith('<canvas_event id="e1" ') and parts == []
    assert [e.id for e in (await w.canvas("s1")).unseen_events()] == ["e2"]

    await _turn_ends(w)
    await realize_next_pending(storage_provider=w.sp, workspace_id="ws-1", session_id="s1", wake_deps=_wake_deps(w))
    texts = [text for text, _parts in w.registry.workspace("ws-1").slots["s1"].appended]
    assert texts[1].startswith('<canvas_event id="e2" ') and SECRET_NOTE in texts[1]
    assert [r["payload"][CANVAS_EVENT_PAYLOAD_KEY]["id"] for r in w.records("s1", "user_input")] == ["e1", "e2"]
    assert (await w.canvas("s1")).unseen_events() == []


async def test_a_queued_steer_without_a_canvas_event_is_realized_as_before() -> None:
    w = await _world("running")
    session = await w.sessions.get("s1")
    from primer.session.pending_messages import store_pending_steer

    await store_pending_steer(storage_provider=w.sp, session=session, text="typed", workspace_registry=w.registry,
                              attribution={"canvas_event": "not a dict"})
    await _turn_ends(w)
    await realize_next_pending(storage_provider=w.sp, workspace_id="ws-1", session_id="s1", wake_deps=_wake_deps(w))
    [user_input] = w.records("s1", "user_input")
    assert user_input["payload"]["text"] == "typed" and CANVAS_EVENT_PAYLOAD_KEY not in user_input["payload"]
