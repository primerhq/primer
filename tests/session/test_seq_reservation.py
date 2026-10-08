"""``reserve_seq``: the guarded reservation of the next seq of a session's log (S2a PR-12a, plan 3.8 A3).

``reserve_next_seq`` is the same reservation for an advisory record that has no guard of its own (01a11cd8).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from primer.model.except_ import NotFoundError
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.seq_reservation import reserve_next_seq, reserve_seq

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


# ---- reserve_next_seq ---------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_advisory_reservations_never_share_a_seq():
    sessions = await _sessions()
    real_get = sessions.get

    async def yielding_get(id, *, conn=None):
        row = await real_get(id, conn=conn)
        await asyncio.sleep(0)      # every reservation reads before any patches: the fence is what keeps them apart
        return row

    sessions.get = yielding_get

    taken = await asyncio.gather(*(reserve_next_seq(sessions, "s", attempts=20) for _ in range(8)))

    assert sorted(taken) == list(range(7, 15))
    assert (await sessions.get("s")).last_seq == 14


@pytest.mark.asyncio
async def test_an_advisory_reservation_that_is_overtaken_reads_again_and_takes_the_next_seq():
    """A writer moved last_seq between the read and the patch: the first fence is rejected, the retry lands on the next seq."""
    sessions = await _sessions()
    real_patch_if = sessions.patch_if
    overtaken = []

    async def overtaking_patch_if(*args, **kwargs):
        if not overtaken:
            overtaken.append(True)
            row = await sessions.get("s")
            await real_patch_if("s", {"last_seq": row.last_seq + 1}, where={"last_seq": [row.last_seq]})   # a steer takes seq 7
        return await real_patch_if(*args, **kwargs)

    sessions.patch_if = overtaking_patch_if

    assert await reserve_next_seq(sessions, "s") == 8
    assert (await sessions.get("s")).last_seq == 8


@pytest.mark.asyncio
async def test_an_advisory_reservation_gives_up_after_its_attempts_and_writes_nothing_more():
    sessions = await _sessions()

    async def always_overtaken(*args, **kwargs):
        return None

    sessions.patch_if = always_overtaken

    assert await reserve_next_seq(sessions, "s", attempts=3) is None


@pytest.mark.asyncio
async def test_an_advisory_reservation_for_a_row_that_is_gone_is_none():
    sessions = await _sessions()

    assert await reserve_next_seq(sessions, "gone") is None


@pytest.mark.asyncio
async def test_the_callers_transaction_is_passed_through_to_every_storage_call():
    """The claim adapter reserves inside its release transaction: a call on another connection would wait on the row lock that transaction holds."""
    sessions = await _sessions()
    seen = []
    real_get, real_patch_if = sessions.get, sessions.patch_if

    async def get(id, *, conn=None):
        seen.append(("get", conn))
        return await real_get(id)

    async def patch_if(id, patch=None, *, where, set_paths=None, conn=None):
        seen.append(("patch_if", conn))
        return await real_patch_if(id, patch, where=where, set_paths=set_paths)

    sessions.get, sessions.patch_if = get, patch_if
    transaction = object()

    assert await reserve_next_seq(sessions, "s", conn=transaction) == 7
    assert seen == [("get", transaction), ("patch_if", transaction)]
