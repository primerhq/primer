"""Review 2026-11-23 finding 1.9: a stale turn must not idle-write over the new owner's row.

When the heartbeat loop confirms a lease was taken, it hard-cancels the running turn with
``CANCEL_REASON_PREEMPTED``. The turn's streaming exit re-raises that cancellation (the
session no longer belongs to this worker), but the streaming phase's ``finally`` used to
write ``turn_status`` back to "idle" on the way out regardless. The peer that now holds the
session is mid-turn and has JUST stamped the row "running" with its own ``turn_started_at``,
so the stale turn's idle write wipes the new owner's stamp: the peer's turn runs invisibly
(a reader of the row sees "idle") and a wake that arms the next turn can be stomped back to
idle and stranded.

Fixed (review 2026-11-23, 1.9): the non-user-cancel re-raise sets ``unlanded_cancel`` and the
finally skips its idle write - the row is left exactly as the new owner stamped it. The pool's
drain-cut path relies on the same flag: a drain must leave ``turn_status`` as a crash leaves
it (review 2026-11-23, 1.1).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.model.chat import TextDelta
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import CANCEL_REASON_PREEMPTED
from primer.session import dispatch
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from tests.conftest import _FakeStorageProvider
from tests.session.test_dispatch import FakeWorkspaceIO, _make_lease

SID = "s1"


class _BlockingExecutor:
    """Yields one delta, then blocks mid-stream: where a preempt lands."""

    def __init__(self) -> None:
        self.streaming = asyncio.Event()

    async def invoke(self, messages: list[Any], **kwargs: Any):
        self.streaming.set()
        yield TextDelta(text="a", index=0)
        await asyncio.Event().wait()


async def _seed(sp) -> WorkspaceSession:
    row = WorkspaceSession(
        id=SID,
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="a"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_status="idle",
    )
    return await sp.get_storage(WorkspaceSession).create(row)


@pytest.mark.asyncio
async def test_a_lost_lease_does_not_idle_write_over_the_new_owners_row():
    """A turn cut by the lost-lease cancel leaves the peer's fresh "running" stamp in place."""
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    await _seed(sp)
    bus = InMemoryEventBus()
    await bus.initialize()
    io = FakeWorkspaceIO()
    executor = _BlockingExecutor()

    async def _build(_s):
        return executor

    task = asyncio.create_task(run_one_session_turn(
        _make_lease(SID),
        SessionDispatchDeps(
            storage_provider=sp, workspace_io=io, event_bus=bus, build_executor=_build,
        ),
    ))
    try:
        await asyncio.wait_for(executor.streaming.wait(), timeout=5.0)
        # The peer's fresh claim: the same write a new claim makes for its own turn start
        # (the production _flip_to_running, guarded on the row being live).
        row = await sessions.get(SID)
        peer_stamp = datetime.now(timezone.utc)
        written, _ = await dispatch._flip_to_running(sessions, row, peer_stamp)
        assert written is not None, "the peer's running stamp must land on the live row"

        # The heartbeat's lost-lease cancel: hard, with the lost-lease reason.
        task.cancel(CANCEL_REASON_PREEMPTED)
        with pytest.raises(asyncio.CancelledError):
            await task

        after = await sessions.get(SID)
        assert after.status == SessionStatus.RUNNING
        assert after.turn_status == "running", (
            f"the stale turn wrote turn_status={after.turn_status!r} over the new owner's "
            "stamp: the peer's turn runs invisibly and a wake can be stomped back to idle"
        )
        assert after.turn_started_at == peer_stamp, (
            "the stale turn cleared the new owner's turn_started_at"
        )
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (Exception, asyncio.CancelledError):
                pass
        await bus.aclose()
