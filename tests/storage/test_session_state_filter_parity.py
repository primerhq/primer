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
# 0 is a session that never completed a turn, 1 is the usual value after ONE turn (the boundary of ``turn_no > 0``), 3 is "several".
_TURN_NO = (0, 1, 3)
# Every ``status`` is its own pre-image of the rule, so the matrix below is the
# full product: 5 x 3 x 3 x 3 = 135 rows.
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
    assert len(_MATRIX) == 135
    assert all(count >= 6 for count in by_state.values()), by_state


async def _assert_parity(storage, rows: list[WorkspaceSession], *, reload: bool = False) -> None:
    """Every state's predicate returns exactly the rows whose property reads that state, and every row is in exactly one state.

    ``reload`` judges the property on the row as the storage hands it back (a document written before an axis existed reads the axis's default
    there), which is what a list over such a document must agree with.
    """
    for row in rows:
        await storage.create(row)
    if reload:
        rows = [await storage.get(row.id) for row in rows]
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


def test_a_resting_session_reads_parked_from_the_first_completed_turn_on() -> None:
    """The route that is easiest to lose: no park, no turn running, but the session rests WAITING or PAUSED after at least one turn."""
    for status in (SessionStatus.WAITING, SessionStatus.PAUSED):
        for turn_no in (1, 3):
            assert _session("r", status, None, "idle", turn_no).session_state == "parked", (status, turn_no)
        assert _session("r", status, None, "idle", 0).session_state == "waiting", status
        assert _session("r", status, None, "claimable", 1).session_state == "parked", status


# What a stored document gets when its key is missing, per axis: the model's own default.
_AXIS_DEFAULTS = {"turn_status": "idle", "turn_no": 0, "parked_status": None}
_AXIS_COLUMN = {"turn_status": 2, "turn_no": 3, "parked_status": 1}      # index into a _MATRIX combo


@pytest.mark.parametrize("axis", sorted(_AXIS_DEFAULTS))
async def test_a_row_written_before_an_axis_existed_agrees_with_the_property(provider, axis) -> None:  # noqa: F811
    """An older row has no key for the axis at all. The model reads its default; SQL sees NULL, and ``NULL != 'running'`` is not true, so a filter
    that spelled "not running" (or "not resting") as an inequality would drop the row from every state. Each axis is stripped ALONE, from every
    row of the matrix that holds the axis's default, over every combination of the other axes."""
    storage = provider.get_storage(WorkspaceSession)
    rows = _matrix_rows()
    stripped = [
        row.id for row, combo in zip(rows, _MATRIX, strict=True)
        if combo[_AXIS_COLUMN[axis]] == _AXIS_DEFAULTS[axis]
    ]
    for row in rows:
        await storage.create(row)
    if isinstance(provider, SqliteStorageProvider):
        conn = provider.connection
        for sid in stripped:
            await conn.execute(
                f"UPDATE sessions SET data = json_remove(data, '$.{axis}') WHERE id = ?", (sid,),
            )
        await conn.commit()
    else:
        async with provider.pool.acquire() as c:
            await c.execute(
                f'UPDATE "{provider.schema}".sessions SET data = data - \'{axis}\' WHERE id = ANY($1::text[])', stripped,
            )
    loaded = [await storage.get(row.id) for row in rows]
    assert all(row is not None for row in loaded)
    # the stripped documents really lack the key and the model reads the default
    assert len(stripped) >= 45
    for sid in stripped:
        assert getattr(await storage.get(sid), axis) == _AXIS_DEFAULTS[axis]
    for state in _STATES:
        want = {r.id for r in loaded if r.session_state == state}
        assert await _ids(storage, state) == want, (axis, state)
