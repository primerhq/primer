"""The out-of-turn canvas record writer (``primer.canvas.records``): the seq reservation and the file append
are one section, so two concurrent records land in the file in the order their reservations took their seqs
(W57d). A record that lands after a later one is skipped by the tap's seq filter (``seq <= after_seq``), so
the order on the file IS the order the live stream sees.
"""

from __future__ import annotations

import asyncio

import pytest

from primer.canvas.records import append_canvas_record
from primer.model.workspace_session import SessionMessageKind
from tests.canvas._fakes import FakeWorkspace, make_world, seed_session


class _GatedWorkspace(FakeWorkspace):
    """Holds the FIRST line it is given (the earlier-reserved seq) until ``released``, so the second writer
    can land its later-reserved seq in the file first: the out-of-order landing the fix must prevent."""

    def __init__(self) -> None:
        super().__init__()
        self.released = asyncio.Event()
        self.first_is_held = asyncio.Event()
        self._holding = False

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        if not self._holding:
            self._holding = True
            self.first_is_held.set()
            await self.released.wait()
        await super().append_message_line(session_id, line)


@pytest.mark.asyncio
async def test_two_concurrent_records_land_in_the_file_in_their_reservation_order():
    """W57d: two canvas records reserved and appended concurrently take their seqs in order and the file keeps
    that order. Before the fix the second writer's line could land first (its append ran while the first's
    was still in flight), and the tap's seq filter then skipped the record that landed late."""
    world = make_world()
    gated = _GatedWorkspace()
    world.registry.workspaces["ws-1"] = gated
    await seed_session(world)

    async def record(tag: str) -> int | None:
        return await append_canvas_record(
            storage_provider=world.sp,
            workspace_registry=world.registry,
            event_bus=None,
            session_id="s1",
            workspace_id="ws-1",
            kind=SessionMessageKind.CANVAS_EVENT,
            payload={"element": tag},
        )

    first = asyncio.create_task(record("a"))
    await gated.first_is_held.wait()              # the first record has reserved its seq and parked in its file write
    second = asyncio.create_task(record("b"))
    await asyncio.sleep(0.05)                     # the second reserves the next seq and runs its file write while the first is parked
    gated.released.set()
    assert (await first, await second) == (1, 2)  # the reservations took the seqs in order

    seqs = [r["seq"] for r in gated.lines("s1")]
    assert seqs == [1, 2], "the file is in seq order, so the tap's seq filter skips nothing"


@pytest.mark.asyncio
async def test_the_record_is_skipped_and_none_returned_while_a_turn_runs():
    """Characterization (kept with W57d): a reservation may not number a seq a running turn has already
    taken, so while ``turn_status`` is running the record is skipped and nothing is appended or reserved."""
    world = make_world()
    await seed_session(world, turn_status="running")
    seq = await append_canvas_record(
        storage_provider=world.sp,
        workspace_registry=world.registry,
        event_bus=world.bus,
        session_id="s1",
        workspace_id="ws-1",
        kind=SessionMessageKind.CANVAS_EVENT,
        payload={"element": "a"},
    )
    assert seq is None
    assert world.registry.workspace("ws-1").lines("s1") == []
    assert (await world.sessions.get("s1")).last_seq == 0, "no seq was reserved"
