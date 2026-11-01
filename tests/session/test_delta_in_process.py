"""A streamed turn's delta frames go out in process when the deployment says one process serves every reader (perf plan Task 7, finding M7).

Default (``deltas_in_process_only`` off): every frame is ``publish``ed, as before, so a split API / worker deployment still gets live tokens across
processes. On: the delta and phase frames (the ``:delta`` key) use ``publish_local``; ticks and everything else still ``publish``. The operator
sets it only when the process that runs the turn also serves every tap/SSE reader (one API+worker process); nothing here can detect a second
API replica, so the safe default is off.
"""
from __future__ import annotations

import asyncio

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.model.chat import Done, TextDelta
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from tests.session.test_dispatch import FakeExecutor, FakeWorkspaceIO, _make_lease, _seed_session


class _CountingBus(InMemoryEventBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[str] = []
        self.published_local: list[str] = []

    async def publish(self, event_key, payload=None):
        self.published.append(event_key)
        await super().publish(event_key, payload)

    async def publish_local(self, event_key, payload=None):
        self.published_local.append(event_key)
        await super().publish(event_key, payload)  # in memory both reach the same subscribers


async def _run_turn(in_process: bool):
    from tests.conftest import _FakeStorageProvider

    sp = _FakeStorageProvider()
    session = await _seed_session(sp)
    bus = _CountingBus()
    await bus.initialize()
    delivered: list[str] = []
    sub = bus.subscribe(key=f"session:{session.id}:delta")

    async def collect():
        async for event in sub:
            delivered.append(event.payload.get("class") or event.payload.get("class_") or "?")

    collector = asyncio.create_task(collect())

    async def build(_session):
        return FakeExecutor([TextDelta(text="hello", index=0), TextDelta(text=" world", index=0), Done(stop_reason="stop", raw_reason="stop")])

    deps = SessionDispatchDeps(
        storage_provider=sp, workspace_io=FakeWorkspaceIO(), event_bus=bus, build_executor=build, deltas_in_process_only=in_process,
    )
    outcome = await run_one_session_turn(_make_lease(session.id), deps)
    await asyncio.sleep(0.05)
    await sub.aclose()
    await collector
    await bus.aclose()
    assert outcome.success is True
    return bus, delivered, session.id


@pytest.mark.asyncio
async def test_default_publishes_every_frame_through_publish_as_before():
    bus, delivered, sid = await _run_turn(False)
    assert bus.published_local == []
    assert f"session:{sid}:delta" in bus.published and f"session:{sid}:tick" in bus.published
    assert delivered, "the delta frames reach a subscriber"


@pytest.mark.asyncio
async def test_in_process_mode_sends_delta_and_phase_frames_locally_and_only_ticks_through_publish():
    bus, delivered, sid = await _run_turn(True)
    delta_key, tick_key = f"session:{sid}:delta", f"session:{sid}:tick"
    assert delta_key not in bus.published, "no NOTIFY-bound publish of a delta or phase frame"
    assert bus.published_local and set(bus.published_local) == {delta_key}
    assert tick_key in bus.published
    assert all(k.endswith(":tick") or k.endswith(":terminal") for k in bus.published if k.startswith(f"session:{sid}:")), bus.published
    assert delivered, "the same frames still reach a local subscriber"


@pytest.mark.asyncio
async def test_both_modes_deliver_the_same_frames_to_a_local_reader():
    _, default_frames, _ = await _run_turn(False)
    _, local_frames, _ = await _run_turn(True)
    assert default_frames == local_frames
