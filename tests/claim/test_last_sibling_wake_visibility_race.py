"""7a gate verdict unverified item B - concurrent last-two-siblings wake
MISS (the inverse of the double-fire the existing idempotency test
covers).

``ToolCallClaimAdapter._last_sibling_wake_signal`` reads every OTHER
sibling via ``storage.get(sibling_id, conn=conn)`` - deliberately
conn-scoped so it observes state inside THIS release's own transaction
(see that method's own docstring). Under Postgres's default READ
COMMITTED isolation, a conn-scoped read only sees OTHER transactions'
COMMITTED writes - if the LAST TWO siblings release concurrently, each
one's own self-update is inside its OWN still-open transaction when the
OTHER's sibling-check runs, so EACH one observes the OTHER as not-yet-
terminal and returns None. Neither concludes "I am last": the wake is
lost entirely, not merely delayed or double-fired.

Confirmed NOT reachable this way for InMemoryClaimEngine: its storage
fakes have no internal suspension point, so ``asyncio.gather`` always
runs one release to full completion before the other starts (proven by
running the SAME concurrent-release scenario against the in-memory
engine at the bottom of this file and observing exactly one wake, never
zero) - the race is specific to a storage layer with genuine
per-connection write visibility, which this file simulates directly
rather than requiring a live database.

PINNED LIMITATION, not a regression test: the leader-adjudicated
disposition for this finding is accept-and-document rather than fix now
(the real fix moves the "am I last" decision to a post-commit re-check -
an architectural change to WHEN the decision happens, out of scope for
a local patch). ``test_concurrent_last_two_releases_under_read_
committed_isolation_lose_the_wake`` below therefore asserts the CURRENT
lossy behavior on purpose, not the desired one - see
``_last_sibling_wake_signal``'s own docstring for the full trace and
task 01a07c06 (recovery-boot reconciliation, upgraded to a flag-on
prerequisite by this finding) for the eventual fix. When that lands,
flip this test's assertions from "wake lost" to "wake recovered" rather
than deleting it - the scenario it sets up (both transactions open,
each observing the other as non-terminal) is exactly the case the fix
needs to handle.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.int.claim import ReleaseOutcome
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _task(task_id: str, batch: list[str]) -> ToolCallTask:
    return ToolCallTask(
        id=task_id, session_id="s1", turn_no=0, tool_name="t",
        state=ToolCallTaskState.QUEUED, record_seq=1, created_at=_now(),
        batch_task_ids=batch,
    )


class _PerConnVisibilityStorage:
    """Simulates Postgres's own READ COMMITTED isolation: a write made
    under one ``conn`` is visible to reads under that SAME conn
    immediately, but invisible to every OTHER conn (and to conn=None,
    the "committed" view) until ``commit(conn)`` is called."""

    def __init__(self, initial: dict[str, ToolCallTask]) -> None:
        self._committed = dict(initial)
        self._pending: dict[object, dict[str, ToolCallTask]] = {}

    async def get(self, task_id: str, *, conn=None):
        if conn is not None and task_id in self._pending.get(conn, {}):
            return self._pending[conn][task_id]
        return self._committed.get(task_id)

    async def update(self, entity: ToolCallTask, *, conn=None):
        if conn is not None:
            self._pending.setdefault(conn, {})[entity.id] = entity
        else:
            self._committed[entity.id] = entity
        return entity

    def commit(self, conn) -> None:
        for task_id, entity in self._pending.pop(conn, {}).items():
            self._committed[task_id] = entity


@pytest.mark.asyncio
async def test_concurrent_last_two_releases_under_read_committed_isolation_lose_the_wake() -> None:
    """Both releases run inside their OWN open (uncommitted) transaction
    when the OTHER's sibling-check reads run - each sees the other as
    still non-terminal, both return None, and committing afterward does
    not retroactively fire anything. This is the concrete mechanism
    behind item B: a genuine wake loss, not a double-fire."""
    batch = ["A", "B"]
    storage = _PerConnVisibilityStorage({
        "A": _task("A", batch), "B": _task("B", batch),
    })
    adapter = ToolCallClaimAdapter(task_storage=storage)
    conn_a, conn_b = object(), object()

    signal_a = await adapter.on_release(
        conn_a, "A", outcome=ReleaseOutcome(success=True, drop_lease=True),
    )
    signal_b = await adapter.on_release(
        conn_b, "B", outcome=ReleaseOutcome(success=True, drop_lease=True),
    )

    # Neither release observed the other as terminal - the exact race.
    # PINNED, not desired: accept-and-document disposition (7a gate
    # review item B). Flip to "wake recovered" when task 01a07c06
    # (recovery-boot reconciliation) lands.
    assert signal_a is None
    assert signal_b is None

    # Both transactions now commit (as they would in production) - the
    # damage is already done: nothing re-checks after the fact.
    storage.commit(conn_a)
    storage.commit(conn_b)
    assert storage._committed["A"].state == ToolCallTaskState.DONE
    assert storage._committed["B"].state == ToolCallTaskState.DONE
    # No signal was ever produced for either release, so
    # ClaimEngine._post_release_hook (and therefore
    # durably_mark_session_resumable) never runs - the session stays
    # parked forever, absent the park-timeout backstop or a future
    # recovery-boot reconciliation pass (task 01a07c06).


@pytest.mark.asyncio
async def test_sequential_releases_do_not_race_the_second_one_fires() -> None:
    """Control case: when the second release's OWN transaction starts
    strictly after the first one's has already committed (the common,
    non-concurrent case), the race does not occur - the second sibling
    correctly observes the first as terminal and fires."""
    batch = ["A", "B"]
    storage = _PerConnVisibilityStorage({
        "A": _task("A", batch), "B": _task("B", batch),
    })
    adapter = ToolCallClaimAdapter(task_storage=storage)
    conn_a, conn_b = object(), object()

    signal_a = await adapter.on_release(
        conn_a, "A", outcome=ReleaseOutcome(success=True, drop_lease=True),
    )
    storage.commit(conn_a)  # A's transaction commits before B's even starts.
    signal_b = await adapter.on_release(
        conn_b, "B", outcome=ReleaseOutcome(success=True, drop_lease=True),
    )

    assert signal_a is None
    assert signal_b is not None


@pytest.mark.asyncio
async def test_in_memory_engine_concurrent_release_never_loses_the_wake() -> None:
    """Confirms the race is NOT reachable for InMemoryClaimEngine: its
    storage fakes have no internal suspension point, so asyncio.gather
    always runs one release to completion before the other starts -
    exactly one of the two concludes "I am last," never zero."""
    from primer.claim.in_memory import InMemoryClaimEngine
    from primer.int.claim import ClaimKind

    batch = ["x:tool:0:1", "x:tool:0:2"]

    class _Storage:
        def __init__(self) -> None:
            self._data = {tid: _task(tid, batch) for tid in batch}

        async def get(self, task_id: str, *, conn=None):
            return self._data.get(task_id)

        async def update(self, entity: ToolCallTask, *, conn=None):
            self._data[entity.id] = entity
            return entity

    storage = _Storage()
    adapter = ToolCallClaimAdapter(task_storage=storage)
    engine = InMemoryClaimEngine(adapters={ClaimKind.TOOL_CALL: adapter})
    for tid in batch:
        await engine.upsert(ClaimKind.TOOL_CALL, tid)
    leases = await engine.claim_due("worker-A", max_count=2)
    lease_by_id = {lease.entity_id: lease for lease in leases}

    signals: list = []

    async def _hook(signal) -> None:
        signals.append(signal)

    engine.bind_post_release_hook(_hook)

    await asyncio.gather(*[
        engine.release(
            lease_by_id[tid], outcome=ReleaseOutcome(success=True, drop_lease=True),
        )
        for tid in batch
    ])

    assert len(signals) >= 1
