"""``reserve_seq``: the guarded reservation of the next seq of a session's log (S2a PR-12a, plan 3.8 A3)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.model.except_ import NotFoundError
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.seq_reservation import reserve_seq

from tests.conftest import _FakeStorageProvider


async def _sessions(**over):
    fields = dict(
        id="s", workspace_id="w", binding=AgentSessionBinding(agent_id="a"), status=SessionStatus.WAITING,
        created_at=datetime.now(UTC), turn_status="idle", last_seq=6,
    )
    fields.update(over)
    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    await storage.create(WorkspaceSession(**fields))
    return storage


@pytest.mark.asyncio
async def test_a_reservation_takes_the_next_seq_and_is_the_last_seq_write():
    sessions = await _sessions()

    seq = await reserve_seq(sessions, "s", last_seq=6, where={"turn_status": ["idle"]})

    assert seq == 7
    assert (await sessions.get("s")).last_seq == 7


@pytest.mark.asyncio
async def test_a_reservation_from_a_stale_read_is_rejected_and_writes_nothing():
    """A steer took seq 7 after the caller read last_seq 6: the caller must not take 7 as well."""
    sessions = await _sessions(last_seq=7)

    seq = await reserve_seq(sessions, "s", last_seq=6, where={"turn_status": ["idle"]})

    assert seq is None
    assert (await sessions.get("s")).last_seq == 7


@pytest.mark.asyncio
async def test_the_callers_guard_is_part_of_the_reservation():
    sessions = await _sessions(turn_status="claimable")

    assert await reserve_seq(sessions, "s", last_seq=6, where={"turn_status": ["idle"]}) is None
    assert (await sessions.get("s")).last_seq == 6


@pytest.mark.asyncio
async def test_a_guard_naming_last_seq_is_refused():
    sessions = await _sessions()

    with pytest.raises(ValueError, match="last_seq"):
        await reserve_seq(sessions, "s", last_seq=6, where={"last_seq": [6]})


@pytest.mark.asyncio
async def test_a_row_that_is_gone_raises_not_found():
    sessions = await _sessions()

    with pytest.raises(NotFoundError):
        await reserve_seq(sessions, "gone", last_seq=6, where={"turn_status": ["idle"]})
