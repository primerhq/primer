"""Drain checkpoint cursor advancement (S1 P1, plan Task 6 part 1).

Spec: docs/superpowers/ux-revamp/02-s1-design.md section 4. The cursor
advances ONLY at a fully drained checkpoint, to max-seen-seq + 1, never
to a re-read last_seq mid-turn: on the chat surface that let a crash
replay records the previous turn had already consumed.

Plan errata E5: the checkpoint deliberately reads no log. It fires
where the loop already knows the turn terminated, and row.last_seq is
by definition the max seq assigned to this session.
"""

from datetime import UTC, datetime

from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.dispatch import _advance_drain_cursor


class _Sessions:
    def __init__(self, row):
        self.row = row
        self.updates = 0

    async def get(self, _sid):
        return self.row

    async def update(self, row):
        self.row = row
        self.updates += 1


def _row(**kw) -> WorkspaceSession:
    base = {
        "id": "s",
        "workspace_id": "w",
        "binding": AgentSessionBinding(agent_id="a"),
        "status": SessionStatus.RUNNING,
        "created_at": datetime.now(UTC),
    }
    base.update(kw)
    return WorkspaceSession(**base)


async def test_advances_to_last_seq_plus_one():
    sessions = _Sessions(_row(last_seq=7, next_unprocessed_seq=0))
    await _advance_drain_cursor(sessions, "s")
    assert sessions.row.next_unprocessed_seq == 8


async def test_never_moves_backwards():
    """A concurrent writer may already have advanced it further."""
    sessions = _Sessions(_row(last_seq=3, next_unprocessed_seq=99))
    await _advance_drain_cursor(sessions, "s")
    assert sessions.row.next_unprocessed_seq == 99
    assert sessions.updates == 0  # no pointless write


async def test_missing_row_is_a_noop():
    class _Gone:
        async def get(self, _sid):
            return None

        async def update(self, _row):  # pragma: no cover - must not run
            raise AssertionError("must not update a deleted session")

    await _advance_drain_cursor(_Gone(), "s")


async def test_idempotent_when_already_current():
    sessions = _Sessions(_row(last_seq=5, next_unprocessed_seq=6))
    await _advance_drain_cursor(sessions, "s")
    assert sessions.row.next_unprocessed_seq == 6
    assert sessions.updates == 0
