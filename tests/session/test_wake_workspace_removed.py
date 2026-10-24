"""A wake that races the delete of its workspace answers a clean not-found, never an internal error (e2e ``test_t0716_workspace_delete_racing_steer_clean_envelopes``).

``WorkspaceRegistry.destroy`` tears the runtime down (the cached instance goes, the sessions on it end, the directory is removed), ends the sessions'
rows, and only then deletes the workspace row; none of that is serialised against a caller that already holds the live workspace. The writes of
``wake_session`` that follow (reopening a stale slot, the instruction into the slot's FIFO, the USER_INPUT record, the reopen of an ended session) can
therefore fail because the workspace went away under them. A filesystem error was already reported as "the workspace was removed while writing"
(404); the local state repository's other failure, a git command that exits non-zero in a directory that is no longer there, is a ``RuntimeError``
and escaped as ``/errors/internal`` (500).

These tests make the race deterministic: the workspace is removed from the registry INSIDE the write that fails, exactly where a delete lands.
"""

from __future__ import annotations

import pytest

from primer.model.except_ import ConflictError, NotFoundError
from primer.model.workspace_session import SessionStatus
from primer.session.enqueue import wake_session
from primer.session.reset import SessionResetDeps, reset_session
from primer.workspace.local.state import _GitCommandError
from tests.session._peer_fakes import make_world, records, seed_session, slot

# The words main has always used for this (the quirk of its quoting included): the steer tool keeps them in its own answer.
REMOVED = "workspace 'ws-1' was removed while writing session 'ses-b''s instruction"


def _git_failure() -> _GitCommandError:
    """What ``LocalStateRepo`` raises when git exits non-zero in a directory the destroy has taken away: a RuntimeError, not an OSError."""
    return _GitCommandError(("add", "--", "sessions/ses-b/messages.jsonl"), 128, "fatal: not a git repository (or any of the parent directories): .git")


async def _wake(world, **kw):
    return await wake_session(
        workspace_id="ws-1", session_id="ses-b", instruction="steer me", human_intent=kw.pop("human_intent", True), deps=world.wake_deps(), **kw,
    )


def _fail_the_slot_write(world, failure, *, how: str) -> None:
    """Make the slot's instruction write fail the way a delete makes it fail, after the delete has taken the workspace away.

    ``how`` is where the delete is: ``"row gone"`` (the destroy finished, the row is deleted) or ``"destroying"`` (the runtime is gone, the destroy is
    still running, the row is still there)."""

    async def append_instruction(content, *, extra_parts=None):
        if how == "row gone":
            world.registry.remove("ws-1")
        else:
            world.registry.destroying.add("ws-1")
        raise failure

    slot(world, "ses-b").append_instruction = append_instruction


async def _assert_nothing_was_armed(world) -> None:
    row = await world.sessions.get("ses-b")
    # The wake reserves its USER_INPUT's seq before the write (audit G7-03 (c)); a write that failed leaves that seq a gap (a write that timed out
    # may still land at it, so it is never handed out again), which readers tolerate. Nothing else moved: no turn armed, no record, no pulse.
    assert row.turn_status == "idle" and row.last_seq in (0, 1)
    assert world.scheduler.enqueued == [] and world.engine.upserts == []
    assert records(world, "ses-b") == []


@pytest.mark.parametrize("how", ["row gone", "destroying"])
async def test_a_git_failure_while_the_workspace_is_being_removed_is_not_found(how):
    world = make_world()
    await seed_session(world, sid="ses-b", turn_no=1)
    _fail_the_slot_write(world, _git_failure(), how=how)
    with pytest.raises(NotFoundError, match="was removed while writing") as info:
        await _wake(world)
    assert str(info.value) == REMOVED
    assert isinstance(info.value.__cause__, _GitCommandError), "the original failure must stay on the chain for the log"
    await _assert_nothing_was_armed(world)


@pytest.mark.parametrize("how", ["row gone", "destroying"])
async def test_a_failure_of_the_record_write_while_the_workspace_is_being_removed_is_not_found(how):
    world = make_world()
    await seed_session(world, sid="ses-b", turn_no=1)

    async def append_message_line(session_id, line):
        if how == "row gone":
            world.registry.remove("ws-1")
        else:
            world.registry.destroying.add("ws-1")
        raise RuntimeError("the workspace connection closed")

    world.registry.workspace("ws-1").append_message_line = append_message_line
    with pytest.raises(NotFoundError, match="was removed while writing"):
        await _wake(world)
    row = await world.sessions.get("ses-b")
    assert row.turn_status == "idle" and world.scheduler.enqueued == []


async def test_a_failure_to_reopen_a_stale_ended_slot_while_the_workspace_is_being_removed_is_not_found():
    """The row says the session is alive and the slot says ENDED (the destroy ended the sessions first): the wake reopens the slot, and that commit fails."""
    world = make_world()
    await seed_session(world, sid="ses-b", turn_no=1)
    the_slot = slot(world, "ses-b")

    async def status():
        return SessionStatus.ENDED

    async def reopen():
        world.registry.destroying.add("ws-1")
        raise _git_failure()

    the_slot.status, the_slot.reopen = status, reopen
    with pytest.raises(NotFoundError, match="was removed while writing"):
        await _wake(world)
    await _assert_nothing_was_armed(world)


async def test_a_failure_that_is_not_the_workspace_going_away_is_not_masked():
    """The same git failure with the workspace alive is a real fault: it stays what it is (a 500 with its traceback), not a 404 about a removal."""
    world = make_world()
    await seed_session(world, sid="ses-b", turn_no=1)

    async def append_instruction(content, *, extra_parts=None):
        raise _git_failure()

    slot(world, "ses-b").append_instruction = append_instruction
    with pytest.raises(_GitCommandError):
        await _wake(world)
    await _assert_nothing_was_armed(world)


async def test_a_failure_of_the_check_itself_is_not_evidence_of_a_removal():
    world = make_world()
    await seed_session(world, sid="ses-b", turn_no=1)

    async def append_instruction(content, *, extra_parts=None):
        raise _git_failure()

    async def broken_row(workspace_id):
        raise RuntimeError("the registry's storage is down")

    slot(world, "ses-b").append_instruction = append_instruction
    world.registry.get_workspace_row = broken_row
    with pytest.raises(_GitCommandError):
        await _wake(world)


@pytest.mark.parametrize("failure", [FileNotFoundError(2, "No such file or directory"), OSError(28, "No space left on device")])
async def test_a_filesystem_error_is_still_not_found(failure):
    """The mapping that was already there: any OSError of the write is reported as the workspace being removed (it does not wait for a registry check)."""
    world = make_world()
    await seed_session(world, sid="ses-b", turn_no=1)

    async def append_instruction(content, *, extra_parts=None):
        raise failure

    slot(world, "ses-b").append_instruction = append_instruction
    with pytest.raises(NotFoundError, match="was removed while writing"):
        await _wake(world)


async def test_a_conflict_from_the_slot_keeps_its_own_status():
    """An ENDED slot refuses an instruction with a ConflictError (409). That is already a clean envelope and is not rewritten, removed workspace or not."""
    world = make_world()
    await seed_session(world, sid="ses-b", turn_no=1)

    async def append_instruction(content, *, extra_parts=None):
        world.registry.remove("ws-1")
        raise ConflictError("cannot append instruction to ENDED session 'ses-b'")

    slot(world, "ses-b").append_instruction = append_instruction
    with pytest.raises(ConflictError, match="ENDED session"):
        await _wake(world)


@pytest.mark.parametrize(
    "failure",
    [FileNotFoundError(2, "No such file or directory"), _git_failure()],
    ids=["a filesystem error", "a git failure"],
)
async def test_the_reopen_of_an_ended_session_is_covered_too(failure):
    """The ENDED branch writes into the workspace as well (the slot reopens, the invocation divider is appended); a delete can land there."""
    world = make_world()
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_no=2, metadata={"invocation": 1})

    async def reopen():
        world.registry.destroying.add("ws-1")
        raise failure

    slot(world, "ses-b").reopen = reopen
    with pytest.raises(NotFoundError, match="was removed while reopening") as info:
        await _wake(world)
    assert "ses-b" in str(info.value)
    row = await world.sessions.get("ses-b")
    assert row.status == SessionStatus.ENDED, "a reopen that failed must leave the session ended"
    assert world.scheduler.enqueued == []


# ---- the database failing under a wake is not the workspace going away -------------------------------------------------------------
#
# The guard that reports an OSError of a write as "the workspace was removed" covers the writes INTO the workspace. The reopen of an ended session
# also reads the workspace row, resolves the workspace and updates the session row, which are calls to the database; a refused pool connection
# raises ConnectionRefusedError, an OSError, and under the guard it became a false 404 about a workspace nobody touched (lead note on #761).


def _refused() -> ConnectionRefusedError:
    """What a pool that cannot reach the database raises (an OSError). A new one per raise: a shared instance would carry every earlier traceback."""
    return ConnectionRefusedError(111, "Connect call failed ('10.0.0.5', 5432)")


async def _seed_ended(world, *, ended_reason: str = "completed") -> None:
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason=ended_reason, turn_no=2, metadata={"invocation": 1})


async def _assert_still_ended(world) -> None:
    row = await world.sessions.get("ses-b")
    assert row.status == SessionStatus.ENDED, "a reopen that failed must leave the session ended"
    assert world.scheduler.enqueued == [] and world.engine.upserts == []


async def test_a_refused_database_connection_on_the_session_row_update_is_not_a_removal():
    world = make_world()
    await _seed_ended(world)

    async def refused(entity, *, conn=None):
        raise _refused()

    world.sessions.update = refused
    with pytest.raises(ConnectionRefusedError):
        await _wake(world)
    await _assert_still_ended(world)


async def test_a_refused_database_connection_on_the_workspace_lost_recheck_is_not_a_removal():
    """A ``workspace_lost`` session reopens only if the workspace row says it is running again, and that read is the database's."""
    world = make_world()
    await _seed_ended(world, ended_reason="workspace_lost")

    async def refused(workspace_id):
        raise _refused()

    world.registry.get_workspace_row = refused
    with pytest.raises(ConnectionRefusedError):
        await _wake(world)
    await _assert_still_ended(world)


@pytest.mark.parametrize("ended", [True, False], ids=["an ended session", "a session that rests"])
async def test_a_refused_database_connection_on_the_workspace_lookup_is_not_a_removal(ended):
    """Resolving the workspace reads its row and its template. A rested session already surfaced this error as it was; the reopen path agrees."""
    world = make_world()
    if ended:
        await _seed_ended(world)
    else:
        await seed_session(world, sid="ses-b", turn_no=1)

    async def refused(workspace_id):
        raise _refused()

    world.registry.get_workspace = refused
    with pytest.raises(ConnectionRefusedError):
        await _wake(world)
    assert world.scheduler.enqueued == [] and world.engine.upserts == []


async def test_a_failure_of_the_divider_write_while_reopening_is_still_not_found():
    """The other half of the boundary: the invocation divider is written INTO the workspace, so an OSError there is still the removal it was."""
    world = make_world()
    await _seed_ended(world)

    async def append_message_line(session_id, line):
        world.registry.destroying.add("ws-1")
        raise OSError(5, "Input/output error")

    world.registry.workspace("ws-1").append_message_line = append_message_line
    with pytest.raises(NotFoundError, match="was removed while reopening") as info:
        await _wake(world)
    assert isinstance(info.value.__cause__, OSError)
    await _assert_still_ended(world)


async def test_reset_session_passes_no_guard_and_reports_a_workspace_error_as_it_is():
    """``reset_session`` shares the reopen body with the wake but never wrapped it: a workspace error reaches its caller untranslated, as before."""
    world = make_world()
    await _seed_ended(world)

    async def reopen():
        raise OSError(5, "Input/output error")

    slot(world, "ses-b").reopen = reopen
    deps = SessionResetDeps(storage_provider=world.sp, workspace_registry=world.registry, event_bus=None)
    with pytest.raises(OSError, match="Input/output error"):
        await reset_session(workspace_id="ws-1", session_id="ses-b", deps=deps, human_intent=True)
    await _assert_still_ended(world)
