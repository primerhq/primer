"""The canvas changes ride the next instruction ``wake_session`` writes (pivot P5, Task 12; plan Decision 9).

A silent canvas event (a Select change, an audio event, a link open) does not wake the agent. ``wake_session`` adds the ones the agent
has not read to any instruction it writes, a typed message, a realized steer or a canvas click, as a second text part: the
``<canvas_changes>`` digest. The USER_INPUT record gets ``payload.canvas_digest`` (the events without what the user entered: the lead's
ruling 1 on S5), and the wake marks read what it handed the agent and nothing else (audit G8-07, the lead's ruling on #795): the
digest's lines and the click it delivers. An event the digest leaves out stays unread: one past the ten lines, and a click still queued,
which reaches the agent as its own block later.

A canvas never fails a wake (the lead's ruling 4): a digest that cannot be read, or read marks that cannot be written, are logged and
the wake lands as it would without a canvas.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from primer.canvas.blocks import MAX_DIGEST_LINES
from primer.canvas.delivery import (
    CANVAS_DIGEST_PAYLOAD_KEY,
    CANVAS_EVENT_PAYLOAD_KEY,
    DELIVERY_QUEUED,
    DELIVERY_WOKEN,
    deliver_canvas_event,
    event_attribution,
)
from primer.canvas.lang import parse_program
from primer.model.canvas import SessionCanvas
from primer.model.chat import TextPart
from primer.model.workspace_session import PendingSessionMessage, SessionStatus
from primer.session.enqueue import SessionWakeDeps, SessionWakeReceipt, wake_session
from primer.session.pending_messages import realize_next_pending
from tests.canvas._fakes import World, make_world, seed_session

PROGRAM = """
root = Column([controls, viz])
controls = Row([genre, play, stop])
genre = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")
play = Button("Play", "primary")
stop = Button("Stop")
viz = Html("<div id=b>pick one</div>", 60)
"""
INPUT = ("value", "data")


class Scheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, session_id: str) -> None:
        self.enqueued.append(session_id)


class Engine:
    async def upsert(self, *_: Any, **__: Any) -> None:
        return None


async def _world(**row: Any) -> World:
    w = make_world(script_check=lambda body: [])
    await seed_session(w, **({"turn_status": "idle"} | row))
    await w.service.apply(
        session_id="s1", workspace_id="ws-1", statements=parse_program(PROGRAM), unit="line", base_rev=None, source="agent", by="agent",
    )
    return w


def _deps(w: World) -> SessionWakeDeps:
    return SessionWakeDeps(storage_provider=w.sp, scheduler=Scheduler(), claim_engine=Engine(), workspace_registry=w.registry,
                           event_bus=w.bus)


async def _wake(w: World, text: str = "go on", **kwargs: Any):
    return await wake_session(workspace_id="ws-1", session_id="s1", instruction=text, deps=_deps(w), human_intent=True, **kwargs)


async def _change(w: World, value: str = "Lo-fi"):
    return await w.service.record_event(session_id="s1", workspace_id="ws-1", element="genre", kind="change", value=value, by="alice")


async def _click(w: World, element: str = "play"):
    return await w.service.record_event(session_id="s1", workspace_id="ws-1", element=element, kind="click", by="alice")


async def _deliver(w: World, event) -> str:
    return await deliver_canvas_event(
        event=event, state=w.service.state_of(await w.canvas("s1")), session_id="s1", workspace_id="ws-1", storage_provider=w.sp,
        scheduler=Scheduler(), claim_engine=Engine(), workspace_registry=w.registry, event_bus=w.bus,
    )


def _slot_texts(w: World) -> list[list[str]]:
    """The text of every instruction the slot was handed: the instruction, then each extra part."""
    return [[text, *(part.text for part in parts)] for text, parts in w.registry.workspace("ws-1").slots["s1"].appended]


def _keys(blob: Any) -> set[str]:
    if isinstance(blob, dict):
        return set(blob) | {k for v in blob.values() for k in _keys(v)}
    if isinstance(blob, (list, tuple)):
        return {k for v in blob for k in _keys(v)}
    return set()


async def _unread(w: World) -> list[str]:
    return [event.id for event in (await w.canvas("s1")).unseen_events()]


# ---- the digest ----------------------------------------------------------------------------------------------------------------------


async def test_the_digest_rides_the_next_user_input() -> None:
    w = await _world()
    await _change(w)

    await _wake(w)

    assert _slot_texts(w) == [
        ["go on", '<canvas_changes rev="2" count="1">\ne1 genre change value="Lo-fi" by="alice"\n</canvas_changes>'],
    ]
    [user_input] = w.records("s1", "user_input")
    assert user_input["payload"]["text"] == "go on"
    digest = user_input["payload"][CANVAS_DIGEST_PAYLOAD_KEY]
    assert digest == {"rev": 2, "events": [event_attribution((await w.canvas("s1")).events[0])]}
    assert digest["events"][0]["id"] == "e1" and not _keys(digest) & set(INPUT), digest
    assert (await w.canvas("s1")).seen_event_seq == 1

    await _wake(w, "and again")                                   # a second wake carries no digest
    assert _slot_texts(w)[1] == ["and again"]
    assert CANVAS_DIGEST_PAYLOAD_KEY not in w.records("s1", "user_input")[1]["payload"]


async def test_the_digest_part_comes_after_the_attachments() -> None:
    w = await _world()
    await _change(w)
    await _wake(w, extra_parts=[TextPart(text="an attachment")], extra_payload={"attachments": ["a.txt"]})
    texts = _slot_texts(w)[0]
    assert texts[:2] == ["go on", "an attachment"] and texts[2].startswith("<canvas_changes ")
    payload = w.records("s1", "user_input")[0]["payload"]
    assert payload["attachments"] == ["a.txt"] and payload[CANVAS_DIGEST_PAYLOAD_KEY]["rev"] == 2


async def test_a_session_without_a_canvas_or_unread_events_gets_no_digest() -> None:
    w = make_world()
    await seed_session(w, turn_status="idle")
    await _wake(w)
    assert _slot_texts(w) == [["go on"]]

    w = await _world()
    await _wake(w)
    assert _slot_texts(w) == [["go on"]] and CANVAS_DIGEST_PAYLOAD_KEY not in w.records("s1", "user_input")[0]["payload"]


async def test_the_delivered_click_is_not_repeated_in_the_digest() -> None:
    w = await _world()
    await _change(w)
    click = await _click(w)

    assert await _deliver(w, click) == DELIVERY_WOKEN

    [[block, digest]] = _slot_texts(w)
    assert block.startswith('<canvas_event id="e2" ') and 'values: genre="Lo-fi"' in block
    assert digest == '<canvas_changes rev="2" count="1">\ne1 genre change value="Lo-fi" by="alice"\n</canvas_changes>'
    assert "e2" not in digest
    [user_input] = w.records("s1", "user_input")
    assert [e["id"] for e in user_input["payload"][CANVAS_DIGEST_PAYLOAD_KEY]["events"]] == ["e1"]
    assert user_input["payload"][CANVAS_EVENT_PAYLOAD_KEY]["id"] == "e2"
    assert await _unread(w) == [] and (await w.canvas("s1")).seen_event_seq == 2


async def test_a_woken_click_is_marked_read() -> None:
    """The lead's ruling on #795: a later canvas_get or canvas_wait does not return the delivered click as unread."""
    w = await _world()
    assert await _deliver(w, await _click(w)) == DELIVERY_WOKEN
    assert await _unread(w) == [] and (await w.canvas("s1")).seen_event_seq == 1


# ---- what the digest leaves out stays unread (audit G8-07) -----------------------------------------------------------------------------


async def test_events_past_the_digest_lines_stay_unread() -> None:
    w = await _world()
    for n in range(MAX_DIGEST_LINES + 2):
        await _change(w, ["Lo-fi", "Classical"][n % 2])

    await _wake(w)

    digest = _slot_texts(w)[0][1].splitlines()
    assert digest[0] == '<canvas_changes rev="13" count="12">' and digest[-2] == "... and 2 more (call canvas_get)"
    payload = w.records("s1", "user_input")[0]["payload"][CANVAS_DIGEST_PAYLOAD_KEY]
    assert [e["id"] for e in payload["events"]] == [f"e{n}" for n in range(1, 11)]
    assert await _unread(w) == ["e11", "e12"], "only the events the digest listed are marked read"

    await _wake(w, "next")
    assert _slot_texts(w)[1][1].splitlines()[1:3] == ['e11 genre change value="Lo-fi" by="alice"',
                                                      'e12 genre change value="Classical" by="alice"']
    assert await _unread(w) == []


async def test_the_digest_lists_only_silent_events() -> None:
    """The lead's ruling 2 (a) on #892 round 2: a click reaches the agent as its own block, never as a digest line, whether it is queued,
    not delivered yet, or was only recorded (a session that could not take it): the agent reads those with canvas_get."""
    w = await _world()
    await _click(w)                                               # recorded, not delivered (yet)
    await _change(w)
    await w.service.record_event(session_id="s1", workspace_id="ws-1", element="viz", kind="emit", name="pick", by="alice")

    await _wake(w)

    assert _slot_texts(w)[0][1] == '<canvas_changes rev="2" count="1">\ne2 genre change value="Lo-fi" by="alice"\n</canvas_changes>'
    assert await _unread(w) == ["e1", "e3"]


async def test_a_queued_click_is_left_out_of_the_digest_and_stays_unread() -> None:
    """It reaches the agent as its own block when the queue is realized; listing it in a digest first would deliver it twice."""
    w = await _world(turn_status="running", status=SessionStatus.RUNNING)
    assert await _deliver(w, await _click(w)) == DELIVERY_QUEUED
    await _change(w)
    await w.sessions.update((await w.sessions.get("s1")).model_copy(update={"turn_status": "idle"}))

    await _wake(w)

    assert _slot_texts(w)[0][1] == '<canvas_changes rev="2" count="1">\ne2 genre change value="Lo-fi" by="alice"\n</canvas_changes>'
    assert await _unread(w) == ["e1"]


async def test_an_event_read_ahead_by_a_wait_is_not_listed() -> None:
    w = await _world()
    await _change(w)
    await _click(w, "stop")
    await w.service.mark_events_seen("s1", [2])                    # a canvas_wait on stop took e2 and left e1 unread
    await _wake(w)
    assert _slot_texts(w)[0][1].splitlines()[1:-1] == ['e1 genre change value="Lo-fi" by="alice"']
    assert await _unread(w) == [] and (await w.canvas("s1")).seen_event_seq == 2


# ---- a canvas never fails a wake (the lead's ruling 4) ---------------------------------------------------------------------------------


class _Raising:
    """A canvas storage whose every read and write raises: the database the canvas lives in is down."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        async def boom(*_: Any, **__: Any) -> Any:
            raise RuntimeError(f"the canvas table cannot be {name}")
        return boom


async def test_a_failing_canvas_read_never_blocks_a_wake(caplog) -> None:
    w = await _world()
    await _change(w)
    w.sp._stores[SessionCanvas] = _Raising(w.sp.get_storage(SessionCanvas))
    scheduler = Scheduler()

    with caplog.at_level(logging.WARNING, logger="primer.session.enqueue"):
        row = await wake_session(
            workspace_id="ws-1", session_id="s1", instruction="go on", human_intent=True,
            deps=SessionWakeDeps(storage_provider=w.sp, scheduler=scheduler, claim_engine=Engine(), workspace_registry=w.registry,
                                 event_bus=w.bus),
        )

    assert _slot_texts(w) == [["go on"]] and row.turn_status == "claimable" and scheduler.enqueued == ["s1"]
    assert CANVAS_DIGEST_PAYLOAD_KEY not in w.records("s1", "user_input")[0]["payload"]
    assert "canvas digest skipped for s1" in caplog.text


async def test_failing_read_marks_never_block_a_wake(caplog, monkeypatch) -> None:
    w = await _world()
    await _change(w)
    store = w.sp.get_storage(SessionCanvas)

    async def refuse(*_: Any, **__: Any) -> Any:
        raise RuntimeError("the canvas row cannot be written")

    monkeypatch.setattr(store, "patch_if", refuse)
    with caplog.at_level(logging.WARNING, logger="primer.session.enqueue"):
        row = await _wake(w)

    assert _slot_texts(w)[0][1].startswith("<canvas_changes ") and row.turn_status == "claimable"
    assert await _unread(w) == ["e1"], "the events stay unread: the next digest lists them again"
    assert "stayed unread" in caplog.text


class _Hanging:
    """A canvas storage whose ``method`` never answers (a database that took the connection and went quiet); the rest is the real one."""

    def __init__(self, inner: Any, method: str) -> None:
        self._inner, self._method = inner, method

    def __getattr__(self, name: str) -> Any:
        if name == self._method:
            async def hang(*_: Any, **__: Any) -> Any:
                await asyncio.Event().wait()
            return hang
        return getattr(self._inner, name)


def test_the_canvas_reads_of_a_wake_are_bounded_at_two_seconds() -> None:
    from primer.session import enqueue

    assert enqueue._CANVAS_TIMEOUT_S == 2.0


async def test_a_hanging_canvas_read_does_not_hold_the_wake(caplog, monkeypatch) -> None:
    """The lead's ruling 6 on #892 round 2: the digest's read is bounded (2 s, shortened here); a timeout is logged once and skipped."""
    from primer.session import enqueue

    monkeypatch.setattr(enqueue, "_CANVAS_TIMEOUT_S", 0.2)
    w = await _world()
    await _change(w)
    w.sp._stores[SessionCanvas] = _Hanging(w.sp.get_storage(SessionCanvas), "get")

    with caplog.at_level(logging.WARNING, logger="primer.session.enqueue"):
        async with asyncio.timeout(5):
            row = await _wake(w)

    assert _slot_texts(w) == [["go on"]] and row.turn_status == "claimable"
    assert [r.getMessage() for r in caplog.records if "canvas" in r.getMessage()] == [
        "canvas digest skipped for s1: the canvas did not answer within 0.2 s",
    ]


async def test_hanging_read_marks_do_not_hold_the_wake(caplog, monkeypatch) -> None:
    from primer.session import enqueue

    monkeypatch.setattr(enqueue, "_CANVAS_TIMEOUT_S", 0.2)
    w = await _world()
    await _change(w)
    store = w.sp.get_storage(SessionCanvas)
    w.sp._stores[SessionCanvas] = _Hanging(store, "patch_if")

    with caplog.at_level(logging.WARNING, logger="primer.session.enqueue"):
        async with asyncio.timeout(5):
            row = await _wake(w)

    assert _slot_texts(w)[0][1].startswith("<canvas_changes ") and row.turn_status == "claimable"
    assert [r.getMessage() for r in caplog.records if "canvas" in r.getMessage()] == [
        "canvas events [1] of session s1 stayed unread: the canvas did not answer within 0.2 s",
    ]
    w.sp._stores[SessionCanvas] = store
    assert await _unread(w) == ["e1"]


# ---- a click that is queued after all keeps its event ----------------------------------------------------------------------------------


async def test_a_click_that_finds_a_turn_armed_under_the_lock_is_queued_with_its_event() -> None:
    """The routing read happens before the lifecycle lock; a wake that armed the turn in between makes this click a queued steer."""
    w = await _world()
    click = await _click(w)
    await w.sessions.update((await w.sessions.get("s1")).model_copy(update={"turn_status": "claimable"}))
    receipt = SessionWakeReceipt()
    carried = {CANVAS_EVENT_PAYLOAD_KEY: event_attribution(click)}

    await _wake(w, "<canvas_event ...>", extra_payload=carried, receipt=receipt)

    assert receipt.outcome == "queued" and w.records("s1", "user_input") == []
    [queued] = w.sp.get_storage(PendingSessionMessage)._data.values()
    assert queued.attribution == carried
    assert await _unread(w) == ["e1"]


@pytest.mark.parametrize("pause", ["pause_requested", "paused"])
async def test_a_queued_click_waits_out_a_pause_with_its_event(pause: str) -> None:
    """A realized click is a replay (human_intent=False): a pause that landed since leaves it where it is in the queue, with its event,
    and it stays unread until it is realized."""
    w = await _world(turn_status="running", status=SessionStatus.RUNNING)
    assert await _deliver(w, await _click(w)) == DELIVERY_QUEUED
    [queued] = w.sp.get_storage(PendingSessionMessage)._data.values()
    paused = {"pause_requested": True} if pause == "pause_requested" else {"status": SessionStatus.PAUSED, "turn_status": "idle"}
    await w.sessions.update((await w.sessions.get("s1")).model_copy(update=paused))

    assert await realize_next_pending(storage_provider=w.sp, workspace_id="ws-1", session_id="s1", wake_deps=_deps(w)) is False

    [still] = w.sp.get_storage(PendingSessionMessage)._data.values()
    assert (still.id, still.attribution) == (queued.id, queued.attribution)
    assert queued.attribution[CANVAS_EVENT_PAYLOAD_KEY]["id"] == "e1"
    assert w.records("s1", "user_input") == [] and await _unread(w) == ["e1"]
