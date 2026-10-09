"""``last_seq`` is advanced by ONE field-scoped ``patch_if`` fenced on the value read, never by a get + whole-document update (follow-up of #545).

``dispatch._persist_last_seq`` and ``_ResumeDrainTap.finish`` re-read the row and only advanced ``last_seq``, but wrote it back with
``update(fresh.model_copy(...))``: a whole-document write from a snapshot. A writer that moved the row between the read and the write (a steer
that took a higher seq, a park) had its fields erased, ``last_seq`` included: the "advance-only" row went DOWN. The standard (#397) is a
``patch_if`` of the one field, fenced on the value the caller read; a rejected fence re-reads and decides again.

The race is injected the way it happens: another writer advances the row right after the code under test has read it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import primer.session.dispatch as dispatch
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from tests.conftest import _FakeStorageProvider

SID = "s-advance"


def _row(last_seq: int = 5) -> WorkspaceSession:
    return WorkspaceSession(
        id=SID, workspace_id="ws-1", binding=AgentSessionBinding(agent_id="agent-a"), status=SessionStatus.WAITING,
        created_at=datetime.now(UTC), last_seq=last_seq,
    )


async def _sessions(last_seq: int = 5):
    sessions = _FakeStorageProvider().get_storage(WorkspaceSession)
    await sessions.create(_row(last_seq))
    return sessions


def move_the_row_after_the_first_read(sessions, to_seq: int) -> None:
    """Another writer advances ``last_seq`` right after the code under test has read the row."""
    real_get = sessions.get
    real_update = sessions.update
    raced = {"done": False}

    async def get(session_id):
        row = await real_get(session_id)
        if session_id == SID and not raced["done"]:
            raced["done"] = True
            await real_update(row.model_copy(update={"last_seq": to_seq}))
        return row

    sessions.get = get  # type: ignore[method-assign]


def forbid_whole_document_writes(sessions) -> None:
    async def update(*args, **kwargs):
        raise AssertionError("a whole-document update of a session row was used to advance last_seq")

    sessions.update = update  # type: ignore[method-assign]


async def test_persist_last_seq_does_not_pull_back_a_row_that_moved_after_its_read() -> None:
    sessions = await _sessions(5)
    move_the_row_after_the_first_read(sessions, 9)

    await dispatch._persist_last_seq(sessions, SID, 6)

    assert (await sessions.get(SID)).last_seq == 9, "the snapshot write took last_seq back below what another writer committed"


async def test_persist_last_seq_writes_only_that_field() -> None:
    sessions = await _sessions(5)
    forbid_whole_document_writes(sessions)

    await dispatch._persist_last_seq(sessions, SID, 8)

    assert (await sessions.get(SID)).last_seq == 8


async def test_the_helper_advances_a_row_that_is_behind() -> None:
    from primer.session.seq_reservation import advance_last_seq

    sessions = await _sessions(5)
    forbid_whole_document_writes(sessions)

    assert await advance_last_seq(sessions, SID, 8) is True
    assert (await sessions.get(SID)).last_seq == 8


async def test_the_helper_leaves_a_row_that_is_already_ahead() -> None:
    from primer.session.seq_reservation import advance_last_seq

    sessions = await _sessions(9)

    assert await advance_last_seq(sessions, SID, 8) is False
    assert (await sessions.get(SID)).last_seq == 9


async def test_the_helper_re_reads_after_a_rejected_fence_and_does_not_lower_the_row() -> None:
    from primer.session.seq_reservation import advance_last_seq

    sessions = await _sessions(5)
    move_the_row_after_the_first_read(sessions, 9)

    assert await advance_last_seq(sessions, SID, 8) is False        # the fence on 5 was rejected; the re-read saw 9
    assert (await sessions.get(SID)).last_seq == 9


async def test_the_helper_does_nothing_for_a_row_that_is_gone() -> None:
    from primer.session.seq_reservation import advance_last_seq

    sessions = await _sessions(5)

    assert await advance_last_seq(sessions, "no-such-session", 8) is False


async def test_the_helper_does_nothing_for_a_row_deleted_between_its_read_and_its_patch() -> None:
    """The docstring says a row that is gone is not an error: ``patch_if`` raises ``NotFoundError`` for one, and the delete can land right after the read."""
    from primer.session.seq_reservation import advance_last_seq

    sessions = await _sessions(5)
    real_get = sessions.get

    async def get_then_delete(session_id):
        row = await real_get(session_id)
        await sessions.delete(session_id)
        return row

    sessions.get = get_then_delete  # type: ignore[method-assign]

    assert await advance_last_seq(sessions, SID, 8) is False
    assert await real_get(SID) is None
