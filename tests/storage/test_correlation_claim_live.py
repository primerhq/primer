"""Claiming a channel conversation on live Postgres is a conditional insert (Task 72, W6). Live Postgres only."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from primer.channel.correlation import CorrelationStore
from tests.pg_gate import needs_postgres

pg = needs_postgres("channel correlation claim tests")


@pg
@pytest.mark.asyncio
async def test_one_of_many_concurrent_claims_wins_and_the_mapping_takes_its_row(postgres_storage_provider):
    channel_id = f"ch-claim-{uuid.uuid4().hex[:8]}"
    a, b = CorrelationStore(postgres_storage_provider), CorrelationStore(postgres_storage_provider)
    won = await asyncio.gather(*(s.claim_thread(channel_id=channel_id, anchor="th-1") for s in (a, b, a, b)))
    assert sorted(won) == [False, False, False, True]
    await b.upsert_thread_session(channel_id=channel_id, anchor="th-1", workspace_id="w1", session_id="s1")
    assert [(r.session_id, r.workspace_id) for r in await a.list_for_channel(channel_id)] == [("s1", "w1")]
    assert await a.claim_thread(channel_id=channel_id, anchor="th-1") is False
    await a.clear(channel_id, "th-1")


@pg
@pytest.mark.asyncio
async def test_a_claim_beside_a_mapping_written_another_way_is_refused(postgres_storage_provider):
    channel_id = f"ch-claim-{uuid.uuid4().hex[:8]}"
    store = CorrelationStore(postgres_storage_provider)
    await store.upsert_thread_session(channel_id=channel_id, anchor="th-1", workspace_id="w1", session_id="s1")
    assert await store.claim_thread(channel_id=channel_id, anchor="th-1") is False
    await store.release_claim(channel_id=channel_id, anchor="th-1")  # a mapped row is not a claim: kept
    assert [r.session_id for r in await store.list_for_channel(channel_id)] == ["s1"]
    await store.clear(channel_id, "th-1")
