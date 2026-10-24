from primer.int.claim import ClaimKind
from primer.claim.adapters.harnesses import HarnessClaimAdapter


def test_harness_eligibility_sql():
    a = HarnessClaimAdapter(harness_storage=None)
    sql = a.eligibility_sql()
    assert "pending_operation" in sql
    assert "IS NOT NULL" in sql


async def test_a_release_the_dispatch_did_not_finish_clears_the_stamp_with_the_operation():
    """Ticket 01a16a29 (review of #860, R2-5): an INSTALL or SYNC carries the digests of the inputs it was asked for (``operation_inputs``).
    When the worker bails before dispatch releases the row, this adapter finalizes it: the stamp goes with the operation, as in
    ``_release_harness``, so no later operation can be judged against it."""
    from datetime import UTC, datetime

    from primer.int.claim import ReleaseOutcome
    from primer.model.harness import Harness, HarnessOperation, HarnessStatus

    class _Store:
        def __init__(self, row):
            self.row = row

        async def get(self, entity_id, conn=None):
            return self.row

        async def update(self, entity, conn=None):
            self.row = entity
            return entity

    store = _Store(Harness(
        id="h1", slug="hh", name="h", status=HarnessStatus.READY, pending_operation=HarnessOperation.INSTALL,
        operation_inputs={"overrides": "0" * 64}, created_at=datetime.now(UTC),
    ))

    await HarnessClaimAdapter(harness_storage=store).on_release(None, "h1", outcome=ReleaseOutcome(success=False, last_error="worker died"))

    assert (store.row.pending_operation, store.row.operation_inputs, store.row.status) == (None, None, HarnessStatus.ERROR)
