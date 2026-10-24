"""The peer-message ledger does not grow for ever (audit G7-15).

``record_peer_message`` keeps a sender's newest ``PEER_RATE_LIMIT`` rows and trims the rest, but only when that same sender sends again: the rows of a
session that ended, or was deleted, stayed in the table for good. Now the session delete route deletes the rows of the messages the session sent,
and the session-messaging sweep deletes every row older than ``PEER_LEDGER_TTL`` (twice the rate window), so orphans go too. Neither touches a row
the rate check still counts. The route and the sweep run for real over the suite's in-memory storage; every body is bounded by ``asyncio.timeout``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from primer.api.routers.sessions import delete_session
from primer.bus.scheduler_tasks import SessionMessagingSweeper
from primer.model.external_tool import ExternalToolCall
from primer.model.storage import OffsetPage
from primer.model.workspace_session import PeerMessage, SessionStatus
from primer.session.peer_messages import (
    PEER_LEDGER_TTL,
    PEER_RATE_LIMIT,
    PEER_RATE_WINDOW,
    PeerMessageRefused,
    check_peer_rate,
    record_peer_message,
    trim_stale_peer_ledger,
)
from tests.session._peer_fakes import World, make_world, seed_session

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
BOUND_S = 15.0


async def _sent(world: World, sender: str, target: str, at: datetime) -> PeerMessage:
    message = PeerMessage(
        from_session_id=sender, from_workspace_id="ws-1", to_session_id=target, to_workspace_id="ws-1", hops=1, delivery="woken", sent_at=at,
    )
    await record_peer_message(world.sp, message)
    return message


async def _ledger(world: World) -> list[tuple[str, str]]:
    page = await world.sp.get_storage(PeerMessage).find(None, OffsetPage(offset=0, length=200))
    return sorted((row.from_session_id, row.to_session_id) for row in page.items)


async def test_deleting_a_session_deletes_the_ledger_rows_of_the_messages_it_sent():
    """The rows of the messages OTHER sessions sent to it stay: they are those senders' allowance, still counted by their rate check."""
    async with asyncio.timeout(BOUND_S):
        world = make_world()
        await seed_session(world, sid="ses-b", status=SessionStatus.WAITING, turn_no=1)
        for i in range(3):
            await _sent(world, "ses-b", "ses-c", NOW - timedelta(minutes=i))
        await _sent(world, "ses-c", "ses-b", NOW)
        await _sent(world, "ses-c", "ses-d", NOW)

        await delete_session(
            workspace_id="ws-1", session_id="ses-b", force=False, sessions=world.sessions, scheduler=world.scheduler, engine=None,
            workspace_registry=world.registry, event_bus=None, call_storage=world.sp.get_storage(ExternalToolCall), storage_provider=world.sp,
        )

        assert await world.sessions.get("ses-b") is None
        assert await _ledger(world) == [("ses-c", "ses-b"), ("ses-c", "ses-d")]


async def test_a_ledger_that_cannot_be_cleaned_does_not_stop_the_delete(monkeypatch):
    async with asyncio.timeout(BOUND_S):
        world = make_world()
        await seed_session(world, sid="ses-b", status=SessionStatus.WAITING, turn_no=1)
        await _sent(world, "ses-b", "ses-c", NOW)

        async def broken(*_args, **_kwargs):
            raise RuntimeError("the ledger table is unreachable")

        monkeypatch.setattr("primer.session.peer_messages.delete_peer_ledger_of", broken)
        await delete_session(
            workspace_id="ws-1", session_id="ses-b", force=False, sessions=world.sessions, scheduler=world.scheduler, engine=None,
            workspace_registry=world.registry, event_bus=None, call_storage=world.sp.get_storage(ExternalToolCall), storage_provider=world.sp,
        )

        assert await world.sessions.get("ses-b") is None, "the session row is deleted all the same; the TTL sweep takes the ledger row"


async def test_the_sweep_deletes_ledger_rows_past_the_ttl_including_orphans_and_keeps_the_rest():
    """Rows older than ``PEER_LEDGER_TTL`` count for no rate check: they go, whether or not their sender still exists. Rows inside it stay."""
    async with asyncio.timeout(BOUND_S):
        world = make_world()
        now = datetime.now(UTC)
        await _sent(world, "ses-gone", "ses-b", now - PEER_LEDGER_TTL - timedelta(minutes=5))   # an orphan: no such session
        await _sent(world, "ses-a", "ses-b", now - PEER_LEDGER_TTL - timedelta(seconds=1))
        await _sent(world, "ses-a", "ses-c", now - PEER_RATE_WINDOW)
        await _sent(world, "ses-gone-too", "ses-c", now - timedelta(minutes=1))                 # an orphan, still inside the TTL
        sweeper = SessionMessagingSweeper(
            storage_provider=world.sp, scheduler=world.scheduler, claim_engine=None, workspace_registry=world.registry,
        )

        await sweeper._tick()

        assert await _ledger(world) == [("ses-a", "ses-c"), ("ses-gone-too", "ses-c")]


async def test_the_trim_never_deletes_a_row_the_rate_check_counts():
    """A sender at the limit inside the window is still refused after the trim: every row it counts is younger than the TTL."""
    async with asyncio.timeout(BOUND_S):
        world = make_world()
        for i in range(PEER_RATE_LIMIT):
            await _sent(world, "ses-a", "ses-b", NOW - PEER_RATE_WINDOW + timedelta(seconds=1 + i))

        assert await trim_stale_peer_ledger(world.sp, now=NOW) == 0

        assert len(await _ledger(world)) == PEER_RATE_LIMIT
        with pytest.raises(PeerMessageRefused) as refused:
            await check_peer_rate(world.sp, "ses-a", now=NOW)
        assert refused.value.error_type == "rate-limited"


async def test_the_trim_walks_more_than_one_page():
    async with asyncio.timeout(BOUND_S):
        world = make_world()
        storage = world.sp.get_storage(PeerMessage)
        old = NOW - PEER_LEDGER_TTL - timedelta(hours=1)
        for i in range(450):     # more than two pages, from many senders (the per-sender trim keeps at most PEER_RATE_LIMIT each)
            await storage.create(PeerMessage(
                from_session_id=f"ses-{i}", from_workspace_id="ws-1", to_session_id="ses-b", to_workspace_id="ws-1", hops=1,
                delivery="queued", sent_at=old + timedelta(seconds=i),
            ))
        await _sent(world, "ses-a", "ses-b", NOW)

        assert await trim_stale_peer_ledger(world.sp, now=NOW) == 450
        assert await _ledger(world) == [("ses-a", "ses-b")]
