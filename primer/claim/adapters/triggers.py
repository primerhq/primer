"""TriggerClaimAdapter — Spec §12.3.

The lease's ``next_attempt_at`` is set on every upsert to
``trigger.next_fire_at``. The eligibility predicate ensures only
time-based (``delayed`` / ``scheduled``) kinds participate in the claim
loop; event-based kinds (e.g. a future ``channel`` kind) are excluded.

``on_release`` recomputes ``next_fire_at`` from the trigger's source:

* ``delayed``  — one-off; disable + null the next-fire pointer so the
  row never re-fires.
* ``scheduled`` — advance to the next future cron occurrence (in the
  trigger's timezone).
* anything else — null the pointer defensively so the engine doesn't
  re-claim an unsupported kind in a loop.

On Postgres, this hook runs inside the same transaction as the lease
release so the ``next_fire_at`` bump and the lease state stay in sync.
"""

from __future__ import annotations

from datetime import datetime, timezone

from primer.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome
from primer.int.storage import Storage
from primer.model.trigger import Trigger
from primer.trigger.cron import next_fire_at as _cron_next


class TriggerClaimAdapter(ClaimAdapter):
    kind = ClaimKind.TRIGGER
    # Match _table_name_for(Trigger) ("trigger"); see chats.py for why a plural
    # name breaks the Postgres claim query.
    entity_table = "trigger"

    def __init__(self, *, storage: Storage | None) -> None:
        self._storage = storage

    def eligibility_sql(self) -> str:
        # The Trigger model stores ``config`` as a nested object whose
        # discriminator is ``kind``. JSONB-path-wise that's
        # ``data->'config'->>'kind'``; top-level fields use
        # ``data->>'field'`` (matches the harness adapter's pattern).
        return (
            "(e.data->>'enabled')::boolean = true "
            "AND e.data->>'next_fire_at' IS NOT NULL "
            "AND e.data->'config'->>'kind' IN ('delayed', 'scheduled') "
            "AND (e.data->>'next_fire_at')::timestamptz <= now()"
        )

    async def on_release(
        self, conn, entity_id: str, *, outcome: ReleaseOutcome,
    ) -> None:
        if self._storage is None:
            raise RuntimeError(
                "trigger storage is None — cannot run on_release without a storage backend"
            )
        trigger = await self._storage.get(entity_id)
        if trigger is None:
            return
        now = datetime.now(timezone.utc)
        kind = trigger.config.kind
        if kind == "delayed":
            # One-off: disable + null the next-fire pointer so the engine
            # doesn't re-claim it. The trigger row stays for audit.
            updated = trigger.model_copy(update={
                "enabled": False,
                "next_fire_at": None,
            })
        elif kind == "scheduled":
            # Advance the cron to the next future occurrence. We use
            # the source-of-truth helper so timezone + DST handling
            # stay consistent with the source's compute_next_fire_at.
            try:
                nxt = _cron_next(
                    trigger.config.cron,
                    trigger.config.timezone,
                    after=now,
                )
            except Exception:
                # Invalid cron / tz at release time — null the pointer
                # so the engine stops trying. The next config update
                # will reseed it.
                nxt = None
            updated = trigger.model_copy(update={"next_fire_at": nxt})
        else:
            # Unknown / future kind: null the pointer defensively.
            updated = trigger.model_copy(update={"next_fire_at": None})
        await self._storage.update(updated)


__all__ = ["TriggerClaimAdapter"]
