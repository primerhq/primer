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

        async def patch_if(self, entity_id, patch, *, where, conn=None):
            self.row = self.row.model_copy(update=patch)
            return self.row

    store = _Store(Harness(
        id="h1", slug="hh", name="h", status=HarnessStatus.READY, pending_operation=HarnessOperation.INSTALL,
        operation_inputs={"overrides": "0" * 64}, created_at=datetime.now(UTC),
    ))

    await HarnessClaimAdapter(harness_storage=store).on_release(None, "h1", outcome=ReleaseOutcome(success=False, last_error="worker died"))

    assert (store.row.pending_operation, store.row.operation_inputs, store.row.status) == (None, None, HarnessStatus.ERROR)


async def test_a_share_landing_during_the_release_is_not_overwritten(fake_storage_provider):
    """Final review F18: the release writes the operation's fields only, so an owner's share between its read and its write survives."""
    from datetime import UTC, datetime

    from primer.int.claim import ReleaseOutcome
    from primer.model.access import Access
    from primer.model.harness import Harness, HarnessOperation, HarnessStatus

    store = fake_storage_provider.get_storage(Harness)
    await store.create(Harness(
        id="h1", slug="hh", name="h", status=HarnessStatus.READY, pending_operation=HarnessOperation.INSTALL, created_at=datetime.now(UTC),
        access=Access(owners=["u-a"], rev=1),
    ))

    class _Racing:
        """The real storage, with a share landing right after the adapter's read."""

        def __getattr__(self, name):
            return getattr(store, name)

        async def get(self, entity_id, **kw):
            row = await store.get(entity_id, **kw)
            await store.patch_if(entity_id, {"access": Access(owners=["u-a"], grants={"u-b": "read"}, rev=2).model_dump(mode="json")}, where={("access", "rev"): [1]})
            return row

    await HarnessClaimAdapter(harness_storage=_Racing()).on_release(None, "h1", outcome=ReleaseOutcome(success=True))

    row = await store.get("h1")
    assert row.pending_operation is None and row.status == HarnessStatus.READY
    assert row.access.grants == {"u-b": "read"} and row.access.rev == 2
