"""The ``memory`` claim kind's adapter: MemoryJob rows are the claim entity table."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from primer.claim.adapters.memory import MemoryClaimAdapter
from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.model.memory import MemoryJob

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _job(job_id: str = "reflect:a1:s1") -> MemoryJob:
    return MemoryJob(id=job_id, kind="reflect", agent_id="a1", session_id="s1", created_at=NOW, expires_at=NOW + timedelta(days=7))


def test_the_kind_and_the_claim_table():
    adapter = MemoryClaimAdapter(storage=None)
    assert adapter.kind is ClaimKind.MEMORY and ClaimKind.MEMORY.value == "memory"
    assert adapter.entity_table == "memoryjob"  # _table_name_for(MemoryJob): a plural name breaks the Postgres claim query


def test_eligibility_sql_selects_every_existing_job():
    assert MemoryClaimAdapter(storage=None).eligibility_sql() == "TRUE"


async def test_a_job_is_eligible_while_its_row_exists(fake_storage_provider):
    store = fake_storage_provider.get_storage(MemoryJob)
    await store.create(_job())
    adapter = MemoryClaimAdapter(storage=store)
    assert await adapter.is_eligible("reflect:a1:s1", NOW) is True
    assert await adapter.is_eligible("reflect:a1:gone", NOW) is False
    assert await MemoryClaimAdapter(storage=None).is_eligible("anything", NOW) is True, "with no storage the lease stays claimable"


async def test_on_release_has_no_entity_state_to_write(fake_storage_provider):
    store = fake_storage_provider.get_storage(MemoryJob)
    await store.create(_job())
    before = await store.get("reflect:a1:s1")
    assert await MemoryClaimAdapter(storage=store).on_release(None, "reflect:a1:s1", outcome=ReleaseOutcome(success=True)) is None
    assert await store.get("reflect:a1:s1") == before
