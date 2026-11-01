"""TriggerClaimAdapter — Spec §12.3.

The lease's ``next_attempt_at`` is set on every upsert to
``trigger.next_fire_at``. The eligibility predicate ensures only
time-based (``delayed`` / ``scheduled``) kinds participate in the claim
loop; event-based kinds (e.g. a future ``channel`` kind) are excluded.

``on_release`` recomputes ``next_fire_at`` from the trigger's source:

* ``delayed``  - one-off; once its fire finished (:func:`delayed_fire_finished`),
  disable + null the next-fire pointer so the row never re-fires. A fire
  that raised before it finished leaves the row armed.
* ``scheduled`` - advance to the cron occurrence after the tick the claim
  fired (``outcome.fired_tick``; after now when it is not given), in the
  trigger's timezone. A tick that passed during the fire is left due.
* anything else — null the pointer defensively so the engine doesn't
  re-claim an unsupported kind in a loop.

On Postgres, this hook runs inside the same transaction as the lease
release so the ``next_fire_at`` bump and the lease state stay in sync.
"""

from __future__ import annotations

from datetime import datetime, timezone

from primer.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome, read_for_eligibility
from primer.int.storage import Storage
from primer.model.trigger import Trigger
from primer.trigger.cron import next_fire_at as _cron_next


def delayed_fire_finished(trigger: Trigger, *, success: bool) -> bool:
    """Whether a ``delayed`` trigger's one fire is over: the claim succeeded, or the row carries that fire's marker (``last_fired_id``, which
    ``fire_trigger`` records after its fan-out, so a fire whose deliveries failed has it). A fire that raised before then has not, and the
    trigger stays armed for a retry (W48)."""
    from primer.trigger.fire_id import make_fire_id

    return success or trigger.last_fired_id == make_fire_id(trigger.id, trigger.config.fire_at)


class TriggerClaimAdapter(ClaimAdapter):
    kind = ClaimKind.TRIGGER
    # Match _table_name_for(Trigger) ("trigger"); a plural name breaks the
    # Postgres claim query.
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

    async def is_eligible(self, entity_id: str, now: datetime) -> bool:
        """Mirrors :meth:`eligibility_sql`: ``(e.data->>'enabled')::boolean = true AND e.data->>'next_fire_at' IS NOT NULL
        AND e.data->'config'->>'kind' IN ('delayed', 'scheduled') AND (e.data->>'next_fire_at')::timestamptz <= now()``, with
        ``now`` (the engine's clock) for ``now()``.

        A missing row is not eligible (the claim query's inner join); with no storage the lease stays claimable. ``enabled`` is
        read as stored: a document without the key is NULL to the SQL, so not eligible, while the model would default it to
        True; the row as read from storage carries exactly its stored keys (``model_fields_set``), so a missing key is not
        eligible here either. A ``next_fire_at`` without a zone is read as UTC, as the SQL cast reads it under a UTC session
        (the model normalises it on read too; this holds even for a model that did not).
        """
        if self._storage is None:
            return True
        trigger = await read_for_eligibility(self._storage, entity_id)
        if trigger is None:
            return False
        next_fire_at = trigger.next_fire_at
        if next_fire_at is not None and next_fire_at.tzinfo is None:
            next_fire_at = next_fire_at.replace(tzinfo=timezone.utc)
        return (
            "enabled" in trigger.model_fields_set
            and trigger.enabled
            and next_fire_at is not None
            and trigger.config.kind in ("delayed", "scheduled")
            and next_fire_at <= now
        )

    async def on_release(
        self, conn, entity_id: str, *, outcome: ReleaseOutcome,
    ) -> None:
        if self._storage is None:
            raise RuntimeError(
                "trigger storage is None — cannot run on_release without a storage backend"
            )
        trigger = await self._storage.get(entity_id, conn=conn)
        if trigger is None:
            return
        now = datetime.now(timezone.utc)
        kind = trigger.config.kind
        if kind == "delayed":
            if not delayed_fire_finished(trigger, success=outcome.success):
                # Its fire raised before it finished: the row stays armed and the handler requeued the lease with a backoff (W48).
                return
            # One-off: disable + null the next-fire pointer so the engine
            # doesn't re-claim it. The trigger row stays for audit.
            changes: dict = {"enabled": False, "next_fire_at": None}
        elif kind == "scheduled":
            # Advance the cron past the tick this claim fired, not past now: a tick that came while the fire ran stays due, and the next
            # claim applies the trigger's catchup to it (W47). We use the source-of-truth helper so timezone + DST handling stay consistent
            # with the source's compute_next_fire_at.
            try:
                nxt = _cron_next(
                    trigger.config.cron,
                    trigger.config.timezone,
                    after=outcome.fired_tick or now,
                )
            except Exception:
                # Invalid cron / tz at release time — null the pointer
                # so the engine stops trying. The next config update
                # will reseed it.
                nxt = None
            changes = {"next_fire_at": nxt}
        else:
            # Unknown / future kind: null the pointer defensively.
            changes = {"next_fire_at": None}
        # Its own fields only (access-control P3 Decision 8): an edit or a sharing change saved meanwhile survives the release.
        from primer.trigger.row_writes import write_trigger_fields

        await write_trigger_fields(self._storage, trigger, changes, conn=conn)


__all__ = ["TriggerClaimAdapter"]
