"""Tests for the chat-turn claiming primitives.

claim_chats / heartbeat_chat / release_chat on InMemoryScheduler.
Postgres concurrency edge-cases are out of scope for this file.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from matrix.model.chats import Chat
from matrix.scheduler.in_memory import InMemoryScheduler


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_chat(
    chat_id: str,
    *,
    status: str = "active",
    turn_status: str = "idle",
    parked_status: str | None = None,
    claimed_by: str | None = None,
    last_heartbeat_at: datetime | None = None,
) -> Chat:
    return Chat(
        id=chat_id,
        agent_id="agent-1",
        created_at=_utcnow(),
        status=status,  # type: ignore[arg-type]
        turn_status=turn_status,  # type: ignore[arg-type]
        parked_status=parked_status,  # type: ignore[arg-type]
        claimed_by=claimed_by,
        last_heartbeat_at=last_heartbeat_at,
    )


@pytest.fixture
async def scheduler(fake_storage_provider):
    s = InMemoryScheduler(storage_provider=fake_storage_provider)
    await s.initialize()
    yield s
    await s.aclose()


@pytest.fixture
def chat_store(fake_storage_provider):
    from matrix.model.chats import Chat
    return fake_storage_provider.get_storage(Chat)


# ===========================================================================
# TestClaimChats (7 tests)
# ===========================================================================


class TestClaimChats:
    async def test_idle_chat_not_claimed(self, scheduler, chat_store):
        """A chat with turn_status='idle' is NOT claimable."""
        chat = _make_chat("c1", turn_status="idle")
        await chat_store.create(chat)

        leases = await scheduler.claim_chats("w1", max_count=5)

        assert leases == []

    async def test_claimable_chat_is_claimed(self, scheduler, chat_store):
        """A chat with turn_status='claimable' IS claimed; fields flip correctly."""
        chat = _make_chat("c1", turn_status="claimable")
        await chat_store.create(chat)

        leases = await scheduler.claim_chats("w1", max_count=5)

        assert len(leases) == 1
        assert leases[0].chat_id == "c1"
        assert leases[0].worker_id == "w1"
        assert leases[0].claimed_at is not None

        # Verify the stored row was mutated.
        updated = await chat_store.get("c1")
        assert updated is not None
        assert updated.turn_status == "running"
        assert updated.claimed_by == "w1"
        assert updated.claimed_at is not None
        assert updated.last_heartbeat_at is not None

    async def test_fresh_heartbeat_blocks_other_worker(self, scheduler, chat_store):
        """A chat claimed by worker A with a fresh heartbeat cannot be stolen by B."""
        now = _utcnow()
        chat = _make_chat(
            "c1",
            turn_status="running",
            claimed_by="worker-a",
            last_heartbeat_at=now,  # fresh
        )
        await chat_store.create(chat)

        leases = await scheduler.claim_chats(
            "worker-b",
            max_count=5,
            heartbeat_stale_after=timedelta(seconds=90),
        )

        assert leases == []

    async def test_stale_heartbeat_allows_reclaim(self, scheduler, chat_store):
        """A stale heartbeat (beyond the threshold) allows another worker to reclaim."""
        old_hb = _utcnow() - timedelta(seconds=120)  # 2 minutes ago → stale
        chat = _make_chat(
            "c1",
            turn_status="claimable",
            claimed_by="worker-a",
            last_heartbeat_at=old_hb,
        )
        await chat_store.create(chat)

        leases = await scheduler.claim_chats(
            "worker-b",
            max_count=5,
            heartbeat_stale_after=timedelta(seconds=90),
        )

        assert len(leases) == 1
        assert leases[0].worker_id == "worker-b"

    async def test_parked_chat_not_claimed(self, scheduler, chat_store):
        """A chat with parked_status='parked' is NOT claimable even if turn_status='claimable'."""
        chat = _make_chat("c1", turn_status="claimable", parked_status="parked")
        await chat_store.create(chat)

        leases = await scheduler.claim_chats("w1", max_count=5)

        assert leases == []

    async def test_resumable_chat_with_idle_turn_status_is_claimed(
        self, scheduler, chat_store
    ):
        """parked_status='resumable' + turn_status='idle' IS claimed (the resume path)."""
        chat = _make_chat("c1", turn_status="idle", parked_status="resumable")
        await chat_store.create(chat)

        leases = await scheduler.claim_chats("w1", max_count=5)

        assert len(leases) == 1
        assert leases[0].chat_id == "c1"

        updated = await chat_store.get("c1")
        assert updated is not None
        assert updated.turn_status == "running"

    async def test_max_count_caps_returned_leases(self, scheduler, chat_store):
        """max_count limits how many chats are claimed in one call."""
        for i in range(5):
            chat = _make_chat(f"c{i}", turn_status="claimable")
            await chat_store.create(chat)

        leases = await scheduler.claim_chats("w1", max_count=3)

        assert len(leases) == 3


# ===========================================================================
# TestHeartbeatChat (2 tests)
# ===========================================================================


class TestHeartbeatChat:
    async def test_heartbeat_bumps_timestamp_and_returns_true(
        self, scheduler, chat_store
    ):
        """Heartbeat on our own claim bumps last_heartbeat_at and returns True."""
        chat = _make_chat("c1", turn_status="running", claimed_by="w1")
        await chat_store.create(chat)

        result = await scheduler.heartbeat_chat("c1", "w1")

        assert result is True
        updated = await chat_store.get("c1")
        assert updated is not None
        assert updated.last_heartbeat_at is not None

    async def test_heartbeat_returns_false_when_lease_stolen(
        self, scheduler, chat_store
    ):
        """Heartbeat returns False when another worker now holds the claim."""
        chat = _make_chat("c1", turn_status="running", claimed_by="worker-b")
        await chat_store.create(chat)

        result = await scheduler.heartbeat_chat("c1", "worker-a")

        assert result is False


# ===========================================================================
# TestReleaseChat (3 tests)
# ===========================================================================


class TestReleaseChat:
    async def test_release_to_idle_clears_all_claim_fields(
        self, scheduler, chat_store
    ):
        """release_chat(..., next_turn_status='idle') clears all claim fields."""
        now = _utcnow()
        chat = _make_chat(
            "c1",
            turn_status="running",
            claimed_by="w1",
            last_heartbeat_at=now,
        )
        chat = chat.model_copy(update={"claimed_at": now})
        await chat_store.create(chat)

        await scheduler.release_chat("c1", "w1", next_turn_status="idle")

        updated = await chat_store.get("c1")
        assert updated is not None
        assert updated.turn_status == "idle"
        assert updated.claimed_by is None
        assert updated.claimed_at is None
        assert updated.last_heartbeat_at is None

    async def test_release_to_claimable_sets_turn_status(
        self, scheduler, chat_store
    ):
        """release_chat(..., next_turn_status='claimable') sets turn_status to claimable."""
        now = _utcnow()
        chat = _make_chat(
            "c1",
            turn_status="running",
            claimed_by="w1",
            last_heartbeat_at=now,
        )
        await chat_store.create(chat)

        await scheduler.release_chat("c1", "w1", next_turn_status="claimable")

        updated = await chat_store.get("c1")
        assert updated is not None
        assert updated.turn_status == "claimable"
        assert updated.claimed_by is None
        assert updated.claimed_at is None
        assert updated.last_heartbeat_at is None

    async def test_release_is_noop_for_non_owner(self, scheduler, chat_store):
        """release_chat by a non-owner worker silently no-ops."""
        now = _utcnow()
        chat = _make_chat(
            "c1",
            turn_status="running",
            claimed_by="worker-b",
            last_heartbeat_at=now,
        )
        await chat_store.create(chat)

        # worker-a does NOT own the claim; must be a no-op.
        await scheduler.release_chat("c1", "worker-a", next_turn_status="idle")

        updated = await chat_store.get("c1")
        assert updated is not None
        # The row should be unchanged.
        assert updated.claimed_by == "worker-b"
        assert updated.turn_status == "running"
