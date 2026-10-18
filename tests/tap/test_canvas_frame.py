"""The canvas frame: a console learns the canvas changed over the session's delta channel (pivot P5, Task 6; design 8.5).

The frame carries no content, only the state the canvas is in (``rev`` and ``event_seq``): the panel refetches the snapshot from REST.
It rides ``session:{sid}:delta`` like the phase frame, and like it is best effort.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from primer.bus.in_memory import InMemoryEventBus
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.tap.delta import DELTA_EVENT_SUFFIX, CanvasFrame, DeltaClass, publish_canvas_frame
from primer.tap.event import TapEventClass
from primer.tap.router import WorkspaceTapRouter

_TIMEOUT = 2.0


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, event_key: str, payload: dict) -> None:
        self.calls.append((event_key, payload))


class _Down:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, event_key: str, payload: dict) -> None:
        self.calls += 1
        raise RuntimeError("bus down")


async def test_the_canvas_frame_rides_the_delta_channel():
    publish = _Recorder()
    await publish_canvas_frame(publish, session_id="s1", rev=4, event_seq=9)
    assert publish.calls == [("session:s1:delta", {"class": "canvas", "session_id": "s1", "rev": 4, "event_seq": 9})]


async def test_a_failed_publish_is_swallowed():
    publish = _Down()
    await publish_canvas_frame(publish, session_id="s1", rev=1, event_seq=0)      # must not raise
    assert publish.calls == 1


def test_the_canvas_class_is_a_delta_class_and_not_a_tap_class():
    """A delta class is its own namespace: the durable tap classes mirror the record kinds, and a frame is not a record."""
    assert DeltaClass.CANVAS == "canvas"
    assert "canvas" not in {c.value for c in TapEventClass}


def test_the_frame_reads_its_own_wire_form_and_nothing_else():
    frame = CanvasFrame.model_validate({"class": "canvas", "session_id": "s1", "rev": 2, "event_seq": 3})
    assert (frame.class_, frame.session_id, frame.rev, frame.event_seq) == (DeltaClass.CANVAS, "s1", 2, 3)
    assert set(frame.model_dump(by_alias=True, mode="json")) == {"class", "session_id", "rev", "event_seq"}
    with pytest.raises(ValidationError):
        CanvasFrame.model_validate({"class": "phase", "session_id": "s1", "rev": 2, "event_seq": 3})


async def test_the_router_hands_the_frame_to_the_delta_subscribers(fake_storage_provider):
    """The same fan-out the other delta frames take: a canvas frame reaches ``subscribe_delta`` and not the tick subscriber."""
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id="s1", workspace_id="W", binding=AgentSessionBinding(agent_id="a"), status=SessionStatus.WAITING,
        created_at="2026-10-18T12:00:00Z",
    ))
    bus = InMemoryEventBus()
    await bus.initialize()
    router = WorkspaceTapRouter(bus, sessions)
    await router.start()
    try:
        deltas, ticks = router.subscribe_delta("W"), router.subscribe("W")
        try:
            await publish_canvas_frame(bus.publish, session_id="s1", rev=4, event_seq=9)
            frame = await asyncio.wait_for(deltas.__anext__(), timeout=_TIMEOUT)
            assert frame == {"class": "canvas", "session_id": "s1", "rev": 4, "event_seq": 9}
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ticks.__anext__(), timeout=0.2)
        finally:
            await deltas.aclose()
            await ticks.aclose()
    finally:
        await router.aclose()
        await bus.aclose()


def test_the_delta_suffix_is_the_one_the_service_publishes_on():
    assert f"session:s1{DELTA_EVENT_SUFFIX}" == "session:s1:delta"
