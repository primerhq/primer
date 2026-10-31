from __future__ import annotations

from datetime import datetime, UTC

from primer.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome, read_for_eligibility
from primer.int.storage import Storage
from primer.model.harness import HarnessStatus


class HarnessClaimAdapter(ClaimAdapter):
    kind = ClaimKind.HARNESS
    # Match _table_name_for(Harness) ("harness"); a plural name breaks the
    # Postgres claim query.
    entity_table = "harness"

    def __init__(self, *, harness_storage: Storage | None) -> None:
        self._storage = harness_storage

    def eligibility_sql(self) -> str:
        return "e.data->>'pending_operation' IS NOT NULL"

    async def is_eligible(self, entity_id: str, now: datetime) -> bool:
        """Mirrors :meth:`eligibility_sql`, ``e.data->>'pending_operation' IS NOT NULL``.

        A missing row is not eligible (the claim query's inner join); with no storage the lease stays claimable.
        """
        if self._storage is None:
            return True
        harness = await read_for_eligibility(self._storage, entity_id)
        return harness is not None and harness.pending_operation is not None

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        if self._storage is None:
            raise RuntimeError(
                "harness_storage is None - cannot run on_release without a storage backend"
            )
        harness = await self._storage.get(entity_id, conn=conn)
        if harness is None:
            return

        # The dispatch path (run_one_harness_operation -> _release_harness)
        # already writes the operation's authoritative terminal status
        # (INSTALLED / READY / OUTDATED / ERROR) AND clears
        # pending_operation. When that has happened we must NOT clobber it
        # here - a hardcoded READY would, for example, demote a successful
        # INSTALL back to READY. So only finalize the row from this adapter
        # when dispatch did not (pending_operation still set, e.g. the
        # worker bailed before dispatch ran its own release).
        if harness.pending_operation is None:
            return

        now = datetime.now(UTC)
        if outcome.success:
            new_status = HarnessStatus.READY
            last_error = None
        else:
            new_status = HarnessStatus.ERROR
            last_error = outcome.last_error

        # Only the operation's own fields, guarded on the operation still being the one read: a whole-row write from this read would put back an
        # owner's share that landed in between (final review F18).
        patch = {
            "pending_operation": None,
            # The inputs the operation was asked for belong to it (ticket 01a16a29): cleared with it, as _release_harness does.
            "operation_inputs": None,
            "status": new_status.value,
            "last_operation_at": now.isoformat(),
            "last_operation_error": last_error,
        }
        await self._storage.patch_if(
            entity_id, patch, where={"pending_operation": [harness.pending_operation.value]}, conn=conn,
        )
