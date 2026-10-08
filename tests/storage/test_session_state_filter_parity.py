"""The ``session_state`` list filter agrees with the ``session_state`` property.

``WorkspaceSession.session_state`` is derived at read time from four stored
axes (``status``, ``parked_status``, ``turn_status``, ``turn_no``). Filtering a
list by it (C-036: the console's "sessions active: running now" must not count
a parked session) needs the same rule as a storage predicate, which is a second
statement of the rule. This module is what keeps the two from drifting: every
combination of the axes is stored, and each state's predicate must return
exactly the rows whose property reads that state, on every backend and on the
in-memory test double the API tests run over.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

from primer.model.storage import OffsetPage
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
    session_state_predicate,
)
from primer.storage.sqlite import SqliteStorageProvider
from tests.conftest import _InMemoryStorage

# The ``provider`` fixture (sqlite always, Postgres when the gate is set).
from tests.storage.test_storage_contract import provider  # noqa: F401

_STATES = ("waiting", "running", "parked", "ended")
_PARKED = (None, "parked", "resumable")
_TURN = ("idle", "claimable", "running")
_TURN_NO = (0, 3)
# Every ``status`` is its own pre-image of the rule, so the matrix below is the
# full product: 5 x 3 x 3 x 2 = 90 rows.
_MATRIX = list(itertools.product(SessionStatus, _PARKED, _TURN, _TURN_NO))


def _session(sid: str, status, parked, turn, turn_no) -> WorkspaceSession:
    return WorkspaceSession(
        id=sid,
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag"),
        status=status,
        parked_status=parked,
        turn_status=turn,
        turn_no=turn_no,
        created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


def _matrix_rows() -> list[WorkspaceSession]:
    return [
        _session(f"m{i:03d}", *combo) for i, combo in enumerate(_MATRIX)
    ]


async def _ids(storage, state: str) -> set[str]:
    hits = await storage.find(
        session_state_predicate(state), OffsetPage(offset=0, length=200),
    )
    return {s.id for s in hits.items}


def test_the_matrix_reaches_every_state_and_every_branch_of_the_rule() -> None:
    """A parity check over a matrix that never reaches a state proves nothing
    for it: each state is hit, and each by more than one route."""
    by_state: dict[str, int] = {state: 0 for state in _STATES}
    for session in _matrix_rows():
        by_state[session.session_state] += 1
    assert len(_MATRIX) == 90
    assert all(count >= 6 for count in by_state.values()), by_state


async def _assert_parity(storage, rows: list[WorkspaceSession]) -> None:
    for row in rows:
        await storage.create(row)
    seen: set[str] = set()
    for state in _STATES:
        got = await _ids(storage, state)
        want = {r.id for r in rows if r.session_state == state}
        assert got == want, (
            state,
            sorted(got - want),
            sorted(want - got),
        )
        assert not (seen & got), "a row is in two states"
        seen |= got
    assert seen == {r.id for r in rows}, "a row is in no state"


async def test_the_in_memory_double_agrees_with_the_property() -> None:
    await _assert_parity(_InMemoryStorage(WorkspaceSession), _matrix_rows())


async def test_every_backend_agrees_with_the_property(provider) -> None:  # noqa: F811
    await _assert_parity(provider.get_storage(WorkspaceSession), _matrix_rows())


async def test_a_row_written_before_the_axes_existed_reads_waiting_or_ended(
    provider,  # noqa: F811
) -> None:
    """An older row has no ``turn_status``, ``turn_no`` or ``parked_status``
    key at all. The model reads the defaults (idle, 0, none); SQL sees NULL,
    and ``NULL != 'running'`` is not true, so a filter that spelled "not
    running" as an inequality would drop the row from every state."""
    storage = provider.get_storage(WorkspaceSession)
    legacy = [
        _session(f"legacy-{status.value}", status, None, "idle", 0)
        for status in SessionStatus
    ]
    for row in legacy:
        await storage.create(row)
    ids = [r.id for r in legacy]
    if isinstance(provider, SqliteStorageProvider):
        conn = provider.connection
        for sid in ids:
            await conn.execute(
                "UPDATE sessions SET data = json_remove(data, "
                "'$.turn_status', '$.turn_no', '$.parked_status') WHERE id = ?",
                (sid,),
            )
        await conn.commit()
    else:
        async with provider.pool.acquire() as c:
            await c.execute(
                "UPDATE sessions SET data = data - 'turn_status' - 'turn_no' "
                "- 'parked_status' WHERE id = ANY($1::text[])",
                ids,
            )
    # The stripped document really lacks the keys, and the model reads defaults.
    for row in legacy:
        loaded = await storage.get(row.id)
        assert loaded is not None
        assert loaded.turn_status == "idle" and loaded.turn_no == 0
        assert loaded.session_state == row.session_state
    for state in _STATES:
        want = {r.id for r in legacy if r.session_state == state}
        assert await _ids(storage, state) == want, state
    assert await _ids(storage, "waiting") == {
        f"legacy-{s.value}" for s in SessionStatus if s != SessionStatus.ENDED
    }
