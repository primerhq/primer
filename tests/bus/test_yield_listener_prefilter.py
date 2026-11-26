"""6.1: the yield listener pays no session query for bus noise.

The listener subscribes to EVERY bus event. The known noise
topics (token deltas, ticks, hint, harness, rate-limit and invalidation
topics) cost nothing; any other key, including one this module has never
heard of, reaches the session query so its wake is not lost. A mailbox
overflow (the listener fell behind and its oldest events were dropped) logs
the drop count and reads no rows.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.bus.listener import YieldEventListener
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.storage.sqlite import SqliteStorageProvider

pytestmark = pytest.mark.asyncio


class _Event:
    """Minimal stand-in for a bus Event (event_key + payload)."""

    def __init__(self, event_key: str, payload: dict | None = None) -> None:
        self.event_key = event_key
        self.payload = payload or {}


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


def _parked(session_id: str, event_key: str) -> WorkspaceSession:
    now = datetime.now(timezone.utc)
    sess = WorkspaceSession(
        id=session_id,
        workspace_id="ws-x",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
        status=SessionStatus.RUNNING,
        created_at=now,
    )
    sess.parked_status = "parked"
    sess.parked_event_key = event_key
    sess.parked_at = now
    sess.parked_state = {"schema_version": 1, "yielded": {"tool_name": "ask_user"}}
    return sess


@pytest.fixture
async def world(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    storage = provider.get_storage(WorkspaceSession)
    engine = InMemoryClaimEngine(
        adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=storage)},
    )
    yield storage, engine
    await provider.aclose()


# ---------------------------------------------------------------------------
# 6.1a: the cheap pre-query filter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "session:s1:delta",
        "session:s1:tick",
        "session:s1:cancel",
        "session:s1:terminal",
        "session:s1:claimable",
        "events_appended",
        "harness:h1:done",
        "harness-claimable",
        "rate_limit_released:k1",
        "invalidate:toolset:t1",
    ],
)
async def test_each_noise_topic_costs_no_session_query(world, key) -> None:
    storage, engine = world
    await storage.create(_parked("s1", "ask_user:s1:tc1"))
    counting = _CountingStorage(storage)
    listener = YieldEventListener(bus=None, session_storage=counting, engine=engine)

    await listener._handle_event(_Event(key, {"text": "t"}))

    assert counting.find_calls == 0, "bus noise must not cost a session query"
    row = await storage.get("s1")
    assert row.parked_status == "parked"


async def test_an_unknown_key_costs_one_query_and_wakes(world) -> None:
    """A park key minted by code this module does not know must still wake:
    the filter is a deny-list of noise, so an unknown key is queried."""
    storage, engine = world
    key = "brand_new_feature:s1:tc1"
    await storage.create(_parked("s1", key))
    counting = _CountingStorage(storage)
    listener = YieldEventListener(bus=None, session_storage=counting, engine=engine)

    await listener._handle_event(_Event(key, {"response": "x"}))

    assert counting.find_calls >= 1
    row = await storage.get("s1")
    assert row.parked_status == "resumable"


@pytest.mark.parametrize(
    "key",
    [
        "ask_user:s1:tc1",
        "tool_approval:s1:tc1",
        "timer:s1:tc1",
        "evwait:s1:tc1",
        "watch:s1:tc1",
        "mcp_task:ts-1:task-1",
        "trigger:s1:tid-1",
        "tool_wait:task-1",
        "canvaswait:s1:tc1",
        "external_tool:s1:tc1",
    ],
)
async def test_every_park_key_prefix_still_reaches_the_query(world, key) -> None:
    """Every shape a park can wait on flips (``external_tool:`` was the one
    the old allow-list missed)."""
    storage, engine = world
    await storage.create(_parked("s1", key))
    counting = _CountingStorage(storage)
    listener = YieldEventListener(bus=None, session_storage=counting, engine=engine)

    await listener._handle_event(_Event(key, {"response": "x"}))

    assert counting.find_calls >= 1
    row = await storage.get("s1")
    assert row.parked_status == "resumable"


# ---------------------------------------------------------------------------
# 6.1b: the mailbox overflow is logged and reconciled
# ---------------------------------------------------------------------------


class _StallingEngine:
    """Holds the flip open until released: the listener's consumer stalls
    inside the engine re-arm while the bus keeps delivering."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()

    async def mark_resumable(self, *args: Any, **kwargs: Any) -> None:
        await self.gate.wait()


async def test_mailbox_overflow_logs_the_drop_count_and_reads_no_rows(world, caplog) -> None:
    storage, _ = world
    await storage.create(_parked("s1", "ask_user:s1:tc1"))
    bus = InMemoryEventBus(queue_maxsize=5)
    await bus.initialize()
    counting = _CountingStorage(storage)
    engine = _StallingEngine()
    listener = YieldEventListener(bus=bus, session_storage=counting, engine=engine)
    listener.start()
    try:
        # Let the listener's task subscribe before the wake is published:
        # a publish with no subscriber yet is lost (the bus has no replay).
        await asyncio.sleep(0.05)
        # The wake takes the listener; it blocks inside the engine re-arm.
        await bus.publish("ask_user:s1:tc1", {"response": "x"})
        await asyncio.sleep(0.05)
        # The bus's normal traffic overflows the listener's mailbox: the
        # oldest queued events are dropped while the consumer is stalled.
        for i in range(20):
            await bus.publish(f"session:noise:{i}:delta", {})
        await asyncio.sleep(0.1)
        find_calls_before_release = counting.find_calls
        engine.gate.set()  # the consumer's next read fires the loss signal
        await asyncio.sleep(0.3)
    finally:
        await listener.stop()
        await bus.aclose()

    loss = [r for r in caplog.records if "dropped events" in r.getMessage()]
    assert loss, "a dropped wake must be logged, never silent"
    assert loss[0].levelname == "WARNING"
    assert "15 dropped" in loss[0].getMessage(), "the warning names the drop count"
    assert "s1" not in loss[0].getMessage(), "it does not list every parked session"
    assert counting.find_calls == find_calls_before_release, (
        "an overflow must not scan the parked rows (the DB is the likely bottleneck)"
    )
    assert counting.rows_read == 1  # only the wake's own keyed query


async def test_the_listener_reads_the_subscription_drop_counter(world, caplog) -> None:
    """The listener reads the subscription's public ``dropped`` counter, not a
    private mailbox attribute: a subscription exposing only the public counter
    still reports its count (review 2026-11-23, I1)."""
    storage, engine = world
    bus = InMemoryEventBus()
    await bus.initialize()
    listener = YieldEventListener(bus=bus, session_storage=storage, engine=engine)
    try:
        listener._sub = SimpleNamespace(dropped=7)  # public counter only, no _mailbox
        listener._last_loss_log = float("-inf")
        listener._on_loss()
    finally:
        await bus.aclose()

    loss = [r for r in caplog.records if "dropped events" in r.getMessage()]
    assert loss, "a loss signal must be logged"
    assert "7 dropped" in loss[0].getMessage(), (
        f"the public counter's count must reach the log: {loss[0].getMessage()!r}"
    )


async def test_suppressed_overflow_is_logged_at_the_window_end(world, monkeypatch, caplog) -> None:
    """Drops that land INSIDE the log interval used to be logged only when the
    NEXT loss arrived; a consumer that stays stalled never reads again, so its
    drops went unlogged. They are logged at the interval's end instead
    (review 2026-11-23, I1)."""
    monkeypatch.setattr("primer.bus.listener._LOSS_LOG_INTERVAL_SECONDS", 0.3)
    storage, _ = world
    await storage.create(_parked("s1", "ask_user:s1:tc1"))
    bus = InMemoryEventBus(queue_maxsize=5)
    await bus.initialize()
    engine = _StallingEngine()
    listener = YieldEventListener(bus=bus, session_storage=storage, engine=engine)
    listener.start()
    try:
        await asyncio.sleep(0.05)
        await bus.publish("ask_user:s1:tc1", {"response": "x"})  # the consumer stalls in the re-arm
        await asyncio.sleep(0.05)
        for i in range(20):
            await bus.publish(f"session:noise:{i}:delta", {})  # 15 drops (mailbox holds 5)
        listener._on_loss()  # the first signal logs immediately
        for i in range(10, 15):
            await bus.publish(f"session:more:{i}:delta", {})  # 5 more drops
        listener._on_loss()  # inside the interval: suppressed, the flush must log them
        await asyncio.sleep(0.6)
    finally:
        await listener.stop()
        await bus.aclose()

    loss = [r for r in caplog.records if "dropped events" in r.getMessage()]
    assert len(loss) == 2, (
        f"the suppressed drops must surface at the window's end: {[r.getMessage() for r in loss]!r}"
    )
    assert "15 dropped" in loss[0].getMessage()
    assert "5 dropped" in loss[1].getMessage(), "the window-end log names the suppressed count"
