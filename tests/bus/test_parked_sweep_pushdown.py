"""6.4: the leader loops read only the rows they need.

The timer/timeout sweeps select on ``parked_until <= now`` in the
predicate, and the watcher / mcp park lookups select on the key prefix,
so a fleet of idle parked sessions (human gates with far-off deadlines)
costs no rows per tick. The per-key deadline judgement (GR-4) stays in
Python on the rows the predicate lets through.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from primer.bus.mcp_tasks import _find_parked_mcp_task_keys
from primer.bus.scheduler_tasks import (
    TimeoutSweeper,
    _find_due_timer_keys,
    _find_expired_non_timer_keys,
    _iter_parked_sessions_due,
)
from primer.bus.watcher import _find_active_watch_parks
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.storage.sqlite import SqliteStorageProvider

pytestmark = pytest.mark.asyncio


class _CountingStorage:
    """Delegates everything, counts what ``find`` returns."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.find_calls = 0
        self.rows_read = 0

    async def find(self, predicate: Any, page: Any, **kwargs: Any) -> Any:
        self.find_calls += 1
        result = await self._inner.find(predicate, page, **kwargs)
        self.rows_read += len(result.items)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _SpyProvider:
    """One counting wrapper per model, behind the real provider."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self._spies: dict[type, _CountingStorage] = {}

    def get_storage(self, model: type) -> _CountingStorage:
        if model not in self._spies:
            self._spies[model] = _CountingStorage(self._real.get_storage(model))
        return self._spies[model]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parked(session_id: str, event_key: str, parked_until: datetime | None) -> WorkspaceSession:
    sess = WorkspaceSession(
        id=session_id,
        workspace_id="ws-x",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
    )
    sess.parked_status = "parked"
    sess.parked_event_key = event_key
    sess.parked_until = parked_until
    sess.parked_at = _now()
    sess.parked_state = {"schema_version": 1, "yielded": {"tool_name": "ask_user"}}
    return sess


@pytest.fixture
async def world(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    spy = _SpyProvider(provider)
    yield spy
    await provider.aclose()


async def test_idle_parked_rows_are_not_read_by_the_due_sweeps(world) -> None:
    """50 idle parks (human gates, far-off deadlines) plus 2 due timer
    parks: the timer sweep reads the 2 due rows, not the 52."""
    storage = world.get_storage(WorkspaceSession)
    now = _now()
    for i in range(50):
        await storage.create(_parked(f"idle-{i}", f"ask_user:idle-{i}:tc", now + timedelta(hours=1)))
    for i in range(2):
        await storage.create(_parked(f"due-{i}", f"timer:due-{i}:tc", now - timedelta(seconds=1)))

    spy = world.get_storage(WorkspaceSession)
    keys = await _find_due_timer_keys(spy)
    assert sorted(keys) == ["timer:due-0:tc", "timer:due-1:tc"]
    assert spy.rows_read == 2, "idle parks must not be read by the timer sweep"

    # Same for the non-timer sweep on its own fleet. The two due TIMER
    # rows above also match this sweep's predicate (their deadline is
    # past too); the per-key filter in Python sorts them out.
    storage2 = world.get_storage(WorkspaceSession)
    for i in range(50):
        await storage2.create(_parked(f"idle2-{i}", f"evwait:idle2-{i}:tc", now + timedelta(hours=1)))
    await storage2.create(_parked("due2", "ask_user:due2:tc", now - timedelta(seconds=1)))
    spy2 = world.get_storage(WorkspaceSession)
    spy2.rows_read = 0
    assert await _find_expired_non_timer_keys(spy2) == ["ask_user:due2:tc"]
    assert spy2.rows_read == 3, (
        "only due rows: the one due ask_user row plus the two due timer "
        "rows the key filter excludes in Python"
    )


async def test_a_park_without_a_deadline_costs_no_sweep_read(world) -> None:
    """A parked row with no ``parked_until`` has no due key (the tripwire
    logs it); the sweeps no longer even read it."""
    storage = world.get_storage(WorkspaceSession)
    await storage.create(_parked("nodeadline", "timer:nodeadline:tc", None))

    spy = world.get_storage(WorkspaceSession)
    assert await _find_due_timer_keys(spy) == []
    assert spy.rows_read == 0


async def test_the_watcher_reads_only_watch_parks(world) -> None:
    storage = world.get_storage(WorkspaceSession)
    now = _now()
    for i in range(50):
        await storage.create(_parked(f"idle-{i}", f"ask_user:idle-{i}:tc", now + timedelta(hours=1)))
    for i in range(2):
        await storage.create(_parked(f"watch-{i}", f"watch:watch-{i}:tc", now + timedelta(hours=1)))

    scheduler = SimpleNamespace(_storage=world)
    parks = await _find_active_watch_parks(scheduler)
    assert sorted(p["event_key"] for p in parks) == [
        "watch:watch-0:tc", "watch:watch-1:tc",
    ]
    spy = world.get_storage(WorkspaceSession)
    assert spy.rows_read == 2, "the watcher must not read non-watch parks"


async def test_the_mcp_bridge_reads_only_mcp_task_parks(world) -> None:
    """The ``mcp_task:`` prefix contains ``_`` (a LIKE wildcard): the
    predicate must match it literally, and the rest of the parked fleet
    costs no reads."""
    storage = world.get_storage(WorkspaceSession)
    now = _now()
    for i in range(50):
        await storage.create(_parked(f"idle-{i}", f"ask_user:idle-{i}:tc", now + timedelta(hours=1)))
    # A key that a WILDCARD ``_`` would match but the literal prefix must not.
    await storage.create(_parked("wildcard", "mcp_taskX:ts-1:k-1", now + timedelta(hours=1)))
    # ... and the opposite: an unescaped ``_`` in the prefix would match ``mcpXtask:``.
    await storage.create(_parked("wildcard2", "mcpXtask:ts-9:k-9", now + timedelta(hours=1)))
    for i in range(2):
        await storage.create(_parked(f"mcp-{i}", f"mcp_task:ts-{i}:k-{i}", now + timedelta(hours=1)))

    scheduler = SimpleNamespace(_storage=world)
    parks = await _find_parked_mcp_task_keys(scheduler)
    assert sorted(p["event_key"] for p in parks) == [
        "mcp_task:ts-0:k-0", "mcp_task:ts-1:k-1",
    ]
    spy = world.get_storage(WorkspaceSession)
    assert spy.rows_read == 2, "the mcp bridge must not read non-mcp parks"


async def test_a_whole_second_deadline_is_due_against_a_fractional_now(world) -> None:
    """A stored whole second (``...05Z``) sorts AFTER a fractional bound in
    the same second (``...05.3Z``) as text: the row due at ``...05.0`` must
    still be read when the sweep runs at ``...05.3``, not skipped a tick."""
    storage = world.get_storage(WorkspaceSession)
    whole = datetime(2030, 1, 1, 0, 0, 5, tzinfo=timezone.utc)
    await storage.create(_parked("whole", "timer:whole:tc", whole))
    await storage.create(_parked("later", "timer:later:tc", whole + timedelta(seconds=2)))

    spy = world.get_storage(WorkspaceSession)
    due = [s.id async for s in _iter_parked_sessions_due(spy, whole + timedelta(milliseconds=300))]
    assert due == ["whole"]
    # And the row with a fraction in the same second is not missed either.
    await storage.create(_parked("frac", "timer:frac:tc", whole + timedelta(milliseconds=100)))
    due = [s.id async for s in _iter_parked_sessions_due(spy, whole + timedelta(milliseconds=300))]
    assert sorted(due) == ["frac", "whole"]


async def test_the_unwakeable_check_reads_only_rows_missing_a_deadline_or_key(world, caplog) -> None:
    """50 healthy parks and one park with no deadline: the 30 s tripwire
    reads the one, not the 51, and still reports it."""
    storage = world.get_storage(WorkspaceSession)
    now = _now()
    for i in range(50):
        await storage.create(_parked(f"ok-{i}", f"ask_user:ok-{i}:tc", now + timedelta(hours=1)))
    await storage.create(_parked("stuck", "timer:stuck:tc", None))
    keyless = _parked("keyless", "x", now + timedelta(hours=1))
    keyless.parked_event_key = None
    await storage.create(keyless)

    spy = world.get_storage(WorkspaceSession)
    sweeper = TimeoutSweeper(bus=SimpleNamespace(), session_storage=spy)
    await sweeper._report_unwakeable_parks()

    assert spy.rows_read == 2, "healthy parks must not be read by the tripwire"
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("stuck" in m for m in errors) and any("keyless" in m for m in errors)
