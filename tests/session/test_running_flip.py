"""The running flip of ``run_one_session_turn`` is a conditional, field-scoped write (the lead's ruling on #383).

``run_one_session_turn`` decides the ENDED, cancel and pause exits from the row it reads at the top; the flip to
``turn_status="running"`` was a whole-document ``update`` of a second read, with no status check. So a row another process
PAUSED or ENDED after the top read (the pause route writes PAUSED directly on a WAITING or CREATED row, the cancel
route ENDS one) was run anyway: the flip wrote the stale status back over the pause, the turn ran on a paused or ended
session, and a wake committed between the read and the write (a ``last_seq`` the next writer must not reuse) was
reverted. The flip is now one ``patch_if`` of its own fields, ``where status in (created, running, waiting)``; when it is
refused the row is read again and judged like the completed-turn guard judges it: gone is the vanished-before-dispatch
exit, ENDED the ENDED exit, PAUSED the pause exit (``preserve_park``), and a row that reads live again is tried again.

Every competing write is committed AFTER the top read and BEFORE the flip's own write, on the in-memory storage the
dispatch tests use (a competing write REPLACES the stored object, as a real storage's commit does, so the top read keeps
the row it saw).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from primer.int.claim import ReleaseOutcome
from primer.model.chat import Done, TextDelta
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    FakeExecutor,
    _make_lease,
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
    seeded_session,
)


class _Run:
    """One ``run_one_session_turn`` over a seeded session, counting the executors it builds."""

    def __init__(self, session, io, bus, provider) -> None:
        self.session = session
        self.io = io
        self.bus = bus
        self.provider = provider
        self.storage = provider.get_storage(WorkspaceSession)
        self.builds = 0

    async def go(self) -> ReleaseOutcome:
        async def build(_session: WorkspaceSession):
            self.builds += 1
            return FakeExecutor([TextDelta(text="hi", index=0), Done(stop_reason="stop", raw_reason="stop")])

        deps = SessionDispatchDeps(
            storage_provider=self.provider, workspace_io=self.io, event_bus=self.bus, build_executor=build,
        )
        return await run_one_session_turn(_make_lease(self.session.id), deps)

    def commit_after_the_top_read(self, competing: dict | None) -> list:
        """Another process writes the row right after the turn's top read returns (``competing`` as a
        whole-document write from a fresh read, the way the pause and cancel routes write; ``None`` deletes the row).
        The top read still gets the row it saw."""
        real_get = self.storage.get
        fired: list = []

        async def get(id, *, conn=None):
            row = await real_get(id, conn=conn)
            if not fired:
                fired.append(competing)
                if competing is None:
                    await self.storage.delete(id)
                else:
                    await self.storage.update(row.model_copy(update=competing))
            return row

        self.storage.get = get  # type: ignore[method-assign]
        return fired

    def commit_just_before_the_flip_write(self, competing: dict | None) -> list:
        """Another process writes the row between the flip's read and its own write: the flip is told apart by the
        ``agent_phase="thinking"`` stamp only it writes (a whole-document ``update`` of the old code, a ``patch_if``
        of the new)."""
        real_get, real_update, real_patch_if = self.storage.get, self.storage.update, self.storage.patch_if
        fired: list = []

        async def commit() -> None:
            fired.append(competing)
            if competing is None:
                await self.storage.delete(self.session.id)
                return
            row = await real_get(self.session.id)
            await real_update(row.model_copy(update=competing))

        async def update(entity, *args, **kwargs):
            if not fired and getattr(entity, "agent_phase", None) == "thinking":
                await commit()
            return await real_update(entity, *args, **kwargs)

        async def patch_if(id, patch=None, *, where, set_paths=None, conn=None):
            if not fired and (patch or {}).get("agent_phase") == "thinking":
                await commit()
            return await real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)

        self.storage.update = update  # type: ignore[method-assign]
        self.storage.patch_if = patch_if  # type: ignore[method-assign]
        return fired

    async def row(self) -> WorkspaceSession | None:
        return await self.storage.get(self.session.id)


@pytest.fixture
def run(seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider) -> _Run:
    return _Run(seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider)


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_status", ["running", "idle", "claimable"])
async def test_a_row_paused_after_the_top_read_runs_no_turn_and_keeps_its_pause(run, turn_status) -> None:
    """The pause route writes PAUSED directly on a WAITING or CREATED row. The turn's own top read predates it, so the
    pause exit did not fire; the flip must not write the old status back and run the turn on a paused session. The
    pause exit's own release: the lease drops, the park is kept, and a stale ``running`` is healed."""
    fired = run.commit_after_the_top_read(
        {"status": SessionStatus.PAUSED, "turn_status": turn_status, "interrupt_requested": True},
    )

    outcome = await run.go()

    assert len(fired) == 1, "the competing write did not land"
    assert outcome == ReleaseOutcome(success=True, drop_lease=True, preserve_park=True)
    assert run.builds == 0, "the turn ran on a paused row"
    row = await run.row()
    assert row.status == SessionStatus.PAUSED, "the flip wrote the stale status back over the pause"
    assert row.turn_status == ("claimable" if turn_status == "claimable" else "idle"), row.turn_status
    assert row.interrupt_requested is False, "a stale Stop was left on the PAUSED row"
    assert run.io.read_lines(run.session.id) == [], "the turn wrote records"


@pytest.mark.asyncio
async def test_a_row_ended_after_the_top_read_runs_no_turn_and_stays_ended(run) -> None:
    fired = run.commit_after_the_top_read(
        {"status": SessionStatus.ENDED, "ended_reason": "cancelled", "ended_at": datetime.now(UTC)},
    )

    outcome = await run.go()

    assert len(fired) == 1
    assert outcome == ReleaseOutcome(success=True, drop_lease=True)
    assert run.builds == 0, "the turn ran on an ended row"
    row = await run.row()
    assert row.status == SessionStatus.ENDED and row.ended_reason == "cancelled", "the flip resurrected the row"
    assert row.turn_status == "idle", "a stale running was left on the ended row"
    assert run.io.read_lines(run.session.id) == []


@pytest.mark.asyncio
async def test_a_row_deleted_between_the_flips_read_and_its_write_gets_the_vanished_exit(run, caplog) -> None:
    """The completed-turn guard's own first read already answers a row that is gone before it; this is the row deleted
    AFTER the guard and before the flip's write: ``patch_if`` raises ``NotFoundError``, which is the vanished exit."""
    fired = run.commit_just_before_the_flip_write(None)

    with caplog.at_level(logging.WARNING, logger="primer.session.dispatch"):
        outcome = await run.go()

    assert len(fired) == 1
    assert outcome == ReleaseOutcome(success=False, drop_lease=True)
    assert run.builds == 0, "the turn ran on a row that is gone"
    assert await run.row() is None
    assert any("vanished" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_row_reset_to_created_after_the_top_read_still_runs_its_turn(run) -> None:
    """CREATED is not a settled status: the flip is allowed on a created, running or waiting row, as before."""
    run.commit_after_the_top_read({"status": SessionStatus.CREATED, "turn_status": "idle"})

    outcome = await run.go()

    assert outcome.success is True and outcome.drop_lease is True
    assert run.builds == 1


@pytest.mark.asyncio
async def test_a_wake_committed_between_the_flips_read_and_its_write_is_not_erased(run) -> None:
    """The flip writes only its own fields: a ``last_seq`` another process committed between the flip's read and its
    write (a steer's USER_INPUT seq) survives, and the turn's writer is seeded past it instead of reusing it. The old
    whole-document ``update`` wrote the stale ``last_seq`` back, so the turn's records took seqs the steer already
    used and the row's ``last_seq`` regressed."""
    fired = run.commit_just_before_the_flip_write({"last_seq": 40})

    outcome = await run.go()

    assert len(fired) == 1, "the competing write did not land"
    assert outcome.success is True and run.builds == 1
    seqs = [json.loads(line)["seq"] for line in run.io.read_lines(run.session.id)]
    assert seqs and min(seqs) > 40, f"the turn reused a seq a steer had written: {seqs}"
    row = await run.row()
    assert row.last_seq >= max(seqs) and row.last_seq > 40, f"last_seq regressed to {row.last_seq}"


@pytest.mark.asyncio
async def test_a_row_paused_between_the_flips_read_and_its_write_runs_no_turn(run) -> None:
    """The narrowest window: the pause commits after the flip READ the row and before it WROTE. The old update wrote
    the read status back (RUNNING) over the PAUSED; the conditional write is refused by its status term."""
    fired = run.commit_just_before_the_flip_write({"status": SessionStatus.PAUSED})

    outcome = await run.go()

    assert len(fired) == 1
    assert outcome == ReleaseOutcome(success=True, drop_lease=True, preserve_park=True)
    assert run.builds == 0, "the turn ran on a paused row"
    assert (await run.row()).status == SessionStatus.PAUSED


@pytest.mark.asyncio
async def test_a_refusal_while_the_row_reads_live_again_is_tried_again_then_given_back(run, caplog) -> None:
    """A status flipping paused and back between the write and the re-read refuses the flip while the re-read finds a
    live row. It is tried again; a flip refused three times in a row gives the claim back unrun (requeued, not dropped)
    instead of running a turn on a row the guard could not pin."""
    real_patch_if = run.storage.patch_if
    refused: list = []

    async def patch_if(id, patch=None, *, where, set_paths=None, conn=None):
        if (patch or {}).get("agent_phase") == "thinking":
            refused.append(True)
            return None
        return await real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)

    run.storage.patch_if = patch_if  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="primer.session.dispatch"):
        outcome = await run.go()

    assert len(refused) == 3
    assert outcome.success is False and outcome.drop_lease is False
    assert outcome.requeue_after is not None and outcome.requeue_after > timedelta(0)
    assert run.builds == 0, "the turn ran although its running flip was never applied"
    assert any("running flip" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_flip_still_marks_the_turn_running_for_the_whole_turn_on_an_undisturbed_row(run) -> None:
    """The control: no competing write. The row reads RUNNING/running with the phase stamps, the turn runs once."""
    seen: list[tuple[Any, Any, Any]] = []

    async def build(_session: WorkspaceSession):
        row = await run.storage.get(run.session.id)
        seen.append((row.turn_status, row.agent_phase, row.agent_phase_turn_no))
        return FakeExecutor([TextDelta(text="hi", index=0), Done(stop_reason="stop", raw_reason="stop")])

    deps = SessionDispatchDeps(
        storage_provider=run.provider, workspace_io=run.io, event_bus=run.bus, build_executor=build,
    )
    outcome = await run_one_session_turn(_make_lease(run.session.id), deps)

    assert outcome.success is True
    assert seen == [("running", "thinking", run.session.turn_no)]
