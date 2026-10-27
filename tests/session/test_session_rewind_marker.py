"""Rewind marker write: the fenced seq reservation (S2a PR-5b, D-A1-02).

The marker's seq is RESERVED, not seeded from a row read before the log
read: a steer that ran a turn and parked in that window moves last_seq
and sets the park columns, so the reservation's fence (the route's own
idle-unparked guard plus ``last_seq ==`` the row the caller just
re-read) rejects it and NOTHING is written (the route answers 409)
instead of the marker reusing the steer's seq and a whole-row write
erasing the park. Pinned end to end by the probes in
``tests/api/test_session_compact_rewind_races.py``; these pin the marker
write itself.
"""

import json
from datetime import UTC, datetime

import pytest

from primer.model.except_ import ConflictError
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.rewind import append_rewind_marker


def _row(**kw):
    base = {
        "id": "s", "workspace_id": "w",
        "binding": AgentSessionBinding(agent_id="a"),
        "status": SessionStatus.WAITING,
        "created_at": datetime.now(UTC),
        "last_seq": 5,
    }
    base.update(kw)
    return WorkspaceSession(**base)


class _IO:
    def __init__(self):
        self.lines: list[bytes] = []

    async def append_message_line(self, session_id, line):
        self.lines.append(line)


class _Sessions:
    """A session-row storage whose ``patch_if`` honours its predicate, like the real backends (the fence is the guarantee)."""

    def __init__(self, row: WorkspaceSession):
        self.row = row

    async def patch_if(self, id, patch=None, *, where, set_paths=None, conn=None):
        del set_paths, conn
        for key, wanted in where.items():
            if getattr(self.row, key) not in wanted:
                return None
        self.row = self.row.model_copy(update=dict(patch or {}))
        return self.row


class TestRewindMarkerReservation:
    async def test_the_marker_lands_at_the_reserved_seq_and_the_reservation_is_the_last_seq_write(self):
        io = _IO()
        row = _row()
        sessions = _Sessions(row)
        seq = await append_rewind_marker(
            sessions=sessions, row=row, workspace_io=io, to_seq=1, actor="user",
        )
        assert seq == 6
        written = json.loads(io.lines[0].decode())
        assert written["kind"] == "rewind_marker"
        assert written["seq"] == 6
        assert written["payload"] == {"to_seq": 1, "actor": "user"}
        assert sessions.row.last_seq == 6, "the reservation is the row's last_seq write"

    async def test_a_row_that_parked_while_the_log_was_read_rejects_the_reservation_and_writes_nothing(self):
        """The row the route re-reads under the lock already holds the park: the fence rejects it, so no
        marker and no row write."""
        io = _IO()
        moved = _row(last_seq=7, parked_status="parked", parked_state={"kind": "ask_user"})
        sessions = _Sessions(moved)

        with pytest.raises(ConflictError, match="no longer idle"):
            await append_rewind_marker(
                sessions=sessions, row=moved, workspace_io=io, to_seq=1, actor="user",
            )
        assert io.lines == [], "a rejected reservation writes no marker"
        assert (sessions.row.parked_status, sessions.row.last_seq) == ("parked", 7), "the park is not written over"

    async def test_a_row_whose_last_seq_moved_rejects_the_reservation_and_writes_nothing(self):
        """The fence also carries last_seq == the row the caller read: a concurrent writer that only moved
        the cursor rejects it too."""
        io = _IO()
        stale = _row()
        sessions = _Sessions(_row(last_seq=7))

        with pytest.raises(ConflictError, match="no longer idle"):
            await append_rewind_marker(
                sessions=sessions, row=stale, workspace_io=io, to_seq=1, actor="user",
            )
        assert io.lines == [], "a rejected reservation writes no marker"
        assert sessions.row.last_seq == 7
