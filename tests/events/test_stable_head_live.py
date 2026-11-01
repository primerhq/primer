"""A wait_for_event on a live Postgres does not skip an event whose transaction commits after a later one (T108, GS-34).

Postgres takes an event id at INSERT, not at COMMIT. Transaction A inserts event 1 and stays open; transaction B inserts event 2 and commits; the
wait is created now. Seeded at ``max_id()`` (2) its cursor sits above event 1, which then commits and is never delivered. Seeded at the stable head
(0) it is delivered.

Gated on the single Postgres test gate (``tests/pg_gate.py``); listed in ``LANE_FILES``, so the CI Postgres lane runs it in its own process.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import pytest
import pytest_asyncio

from primer.events.dispatcher import EventDispatcher
from primer.model.event import EventSubscription
from primer.model.principal import PrincipalRef
from primer.model.provider import PoolConfig, PostgresConfig
from primer.model.yield_ import ToolContext
from primer.storage.postgres import PostgresStorageProvider
from primer.toolset.events_wait import make_wait_for_event_handler
from tests.pg_gate import explicit_port, postgres_marks, require_postgres_url

_WHAT = "the live stable-head wait test"
pytestmark = [*postgres_marks(_WHAT), pytest.mark.asyncio]
_BOUND_S = 30.0
ADMIN = PrincipalRef(type="user", id="user-1", display="u", role="admin", source="local")


@pytest_asyncio.fixture
async def provider() -> AsyncIterator[PostgresStorageProvider]:
    url = urlparse(require_postgres_url(_WHAT))
    schema = f"shd{uuid.uuid4().hex[:16]}"
    sp = PostgresStorageProvider(PostgresConfig(
        hostname=url.hostname or "localhost", port=explicit_port(url), username=url.username or "primer",
        password=url.password or "",  # type: ignore[arg-type]
        database=(url.path or "/").lstrip("/") or "postgres", db_schema=schema, pool=PoolConfig(min_size=1, max_size=4),
    ))
    async with asyncio.timeout(_BOUND_S):
        await sp.initialize()
        await sp.get_event_store().ensure_schema()
    try:
        yield sp
    finally:
        async with asyncio.timeout(_BOUND_S):
            async with sp.pool.acquire() as c:
                await c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await sp.aclose()


async def test_an_event_that_commits_below_a_later_one_still_reaches_a_new_wait(provider) -> None:
    store = provider.get_event_store()
    async with asyncio.timeout(_BOUND_S):
        async with provider.pool.acquire() as conn_a:
            txn = conn_a.transaction()
            await txn.start()
            first = await store.append(event_type="t.x", conn=conn_a)        # id taken now, visible only at commit
            second = await store.append(event_type="t.x")                    # commits at once, with the higher id
            assert second > first
            ctx = ToolContext(tool_call_id="call_0", session_id="sess-1", workspace_id="w", initiated_by=ADMIN)
            yielded = await make_wait_for_event_handler(provider)({"event_types": ["t.x"]}, ctx=ctx)
            cursor = await store.get_cursor(yielded.resume_metadata["subscription_id"])
            assert cursor is not None and cursor < first                     # not above the event still to commit
            await txn.commit()

        delivered: list[int] = []

        class _Dispatcher(EventDispatcher):
            async def _deliver(self, sub: EventSubscription, event) -> bool:  # type: ignore[override]
                delivered.append(event.id)
                return True

        dispatcher = _Dispatcher(storage_provider=provider, clock=_Clock())
        for _ in range(4):
            await dispatcher.drain_once()
    assert first in delivered


class _Clock:
    """Jumps a minute at every read, so the dispatcher's hole grace (which only guards the cursor it moves) never makes the test wait."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 60.0
        return self.now
