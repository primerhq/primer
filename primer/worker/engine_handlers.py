"""Per-kind engine claim handlers for the worker pool.

Extracted verbatim from :mod:`primer.worker.pool` (no behaviour change). One
function per non-session claim kind (harness / trigger); each takes the
:class:`~primer.worker.pool.WorkerPool` instance as ``pool`` and reads the same
bound deps the original methods did (``pool._storage`` / ``pool._engine`` /
``pool._event_bus`` / ...). The pool keeps thin delegating methods
(``WorkerPool._run_engine_harness`` etc.) so the ``start()`` dispatch table
(``self._run_engine_harness``) and any test monkeypatches still resolve through
the instance.

The SESSION handler (``_run_engine_session``) intentionally stays in
``pool.py``: it is the ``run_one_session_turn`` monkeypatch seam
(``patch("primer.worker.pool.run_one_session_turn")``).

Per-kind dispatch imports stay lazy inside each function so importing this
module (and ``pool``) doesn't pull the harness / trigger dependency
trees at startup — mirroring the original methods.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primer.int.claim import Lease as ClaimLease
    from primer.worker.pool import WorkerPool

logger = logging.getLogger(__name__)




async def run_engine_harness(pool: "WorkerPool", engine_lease: "ClaimLease") -> None:
    """Handle a HARNESS claim from the engine.

    Bridges to run_one_harness_operation via HarnessDispatchDeps.
    Stamps claimed_by on the harness row so heartbeat checks pass during
    long operations.
    """
    from primer.harness.dispatch import HarnessDispatchDeps, run_one_harness_operation
    from primer.int.claim import ReleaseOutcome
    from primer.model.harness import Harness

    # Verify the harness still has a pending operation before dispatching.
    harness_storage = pool._storage.get_storage(Harness)
    harness = await harness_storage.get(engine_lease.entity_id)
    if harness is None or harness.pending_operation is None:
        await pool._release_lease(
            engine_lease, ReleaseOutcome(success=False, drop_lease=True),
        )
        return

    deps = HarnessDispatchDeps(
        storage_provider=pool._storage,
        event_bus=pool._event_bus,
        provider_registry=pool._provider_registry,
        semantic_search_registry=pool._semantic_search_registry,
    )
    success = False
    try:
        await run_one_harness_operation(
            deps,
            harness_id=engine_lease.entity_id,
            worker_id=pool._worker_id,
        )
        success = True
    except Exception:
        logger.exception(
            "engine harness operation for %s raised",
            engine_lease.entity_id,
        )
    finally:
        await pool._release_lease(
            engine_lease, ReleaseOutcome(success=success, drop_lease=True),
        )


def _now() -> datetime:
    """The worker's clock when it handles a claim (a seam: the catchup tests run the trigger handler on a fake one)."""
    return datetime.now(timezone.utc)


def _clock_ticks(trigger, *, now: datetime) -> list[datetime | None]:
    """What one claim of ``trigger`` fires, in order: the ``scheduled_for`` of each fire; the last is the due tick (ticket 01a16ddb, rulings 3, 4).

    * ``scheduled``: the missed ticks its ``catchup`` keeps, then the due tick, each once
      (:func:`primer.trigger.cron.scheduled_ticks_to_fire`). A row whose cron or timezone cannot be read (both are validated when saved) fires
      once, for its ``next_fire_at``: a malformed row never blocks the fire.
    * ``delayed``: its ``fire_at``.
    * a row that is gone or disabled (``fire_trigger`` answers it skipped), or any other kind: one fire with no tick.

    ``[]`` when the claim is early: its tick (a ``scheduled`` trigger's ``next_fire_at``, a ``delayed`` one's ``fire_at``) is more than
    :data:`~primer.trigger.cron.CLOCK_SKEW` ahead of ``now``. A future tick is never fired early.
    """
    from primer.trigger.cron import CLOCK_SKEW, scheduled_ticks_to_fire

    if trigger is None or not trigger.enabled:
        return [None]
    kind = trigger.config.kind
    if kind == "delayed":
        fire_at = trigger.config.fire_at
        return [] if fire_at > now + CLOCK_SKEW else [fire_at]
    if kind != "scheduled":
        return [None]
    try:
        return list(scheduled_ticks_to_fire(
            trigger.config.cron,
            trigger.config.timezone,
            catchup=trigger.config.catchup,
            first_unfired=trigger.next_fire_at,
            now=now,
        ))
    except Exception:
        logger.exception(
            "trigger %s: could not work out its ticks; firing once for its next_fire_at",
            trigger.id,
        )
        return [trigger.next_fire_at]


async def _lease_after_the_fire(pool: "WorkerPool", trigger_id: str) -> tuple[bool, timedelta | None]:
    """How a claim that fired gives its lease back: ``(drop_lease, requeue_after)`` (ticket 01a16ddb, review of #915).

    The lease is requeued to the trigger's next tick, the one the release records (``TriggerClaimAdapter.on_release``: the next cron tick
    after now for a ``scheduled`` trigger), so it is not claimable again before it. Without the requeue the in-memory claim engine, which does
    not evaluate the adapter's eligibility SQL, re-claimed a released trigger at once, and every scheduled trigger re-fired in a loop on a
    SQLite deployment. A trigger with no next tick (a ``delayed`` trigger that fired, a disabled or deleted one, an unreadable cron) drops the
    lease: the save that lets it fire again upserts a new one (``primer.trigger.service._upsert_claim_if_eligible``). A row that cannot be
    read leaves the lease as the engine would.
    """
    from primer.model.trigger import Trigger
    from primer.trigger.cron import next_fire_at

    try:
        trigger = await pool._storage.get_storage(Trigger).get(trigger_id)
    except Exception:
        logger.exception("trigger %s: could not read it to requeue its lease", trigger_id)
        return False, None
    if trigger is None or not trigger.enabled or trigger.config.kind != "scheduled":
        return True, None
    now = _now()
    try:
        upcoming = next_fire_at(trigger.config.cron, trigger.config.timezone, after=now)
    except Exception:
        return True, None
    return False, max(upcoming - now, timedelta(0))


async def run_engine_trigger(pool: "WorkerPool", engine_lease: "ClaimLease") -> None:
    """Handle a TRIGGER claim from the engine.

    Routes the lease to :func:`primer.trigger.dispatch.fire_trigger`,
    which fans out to each enabled subscription's dispatcher. The
    ``TriggerClaimAdapter.on_release`` hook advances ``next_fire_at``
    (cron tick for ``scheduled``, null/disabled for ``delayed``), and the
    lease is requeued to that next tick (:func:`_lease_after_the_fire`).

    Every fire carries the tick it is for as ``scheduled_for`` (:func:`_clock_ticks`). A ``scheduled`` trigger fires the missed ticks its
    ``catchup`` keeps (``none`` none, ``one`` the most recent, ``all`` each, at most 64, the oldest first), then the due tick once; a missed tick
    is one from the trigger's ``next_fire_at`` up to the due tick (ticket 01a16ddb, rulings 3 and 4). A catch-up fire that raises is logged
    and the next one runs; the due tick's own errors reach the outer except.

    An early claim (its tick more than ``CLOCK_SKEW`` ahead of the worker's clock) fires nothing and hands the lease back untouched
    (``entity_noop``: the release must not advance or disable a trigger that did not fire), requeued to its tick.
    """
    from primer.int.claim import ReleaseOutcome
    from primer.model.trigger import Trigger
    from primer.trigger.dispatch import fire_trigger
    from primer.trigger.subscribers import DispatchDeps

    deps = DispatchDeps(
        storage_provider=pool._storage,
        claim_engine=pool._engine,
        scheduler=pool._scheduler,
        workspace_registry=getattr(pool, "_workspace_registry", None),
        event_bus=pool._event_bus,
    )

    def _failed_deliveries(fire_result) -> int:
        # fire_trigger isolates every per-subscription failure and
        # RETURNS it (ok=False) instead of raising, so "it returned"
        # says nothing about whether anything was delivered.
        return sum(
            1 for r in getattr(fire_result, "results", None) or []
            if not r.get("ok")
        )

    success = False
    failed_deliveries = 0
    replay_errors = 0
    last_error: str | None = None
    early_until: datetime | None = None
    now = _now()
    try:
        trigger = await pool._storage.get_storage(Trigger).get(engine_lease.entity_id)
        ticks = _clock_ticks(trigger, now=now)
        if not ticks:
            early_until = trigger.config.fire_at if trigger.config.kind == "delayed" else trigger.next_fire_at
            logger.info(
                "trigger %s: claimed before its tick %s; firing nothing, requeued to it",
                engine_lease.entity_id, early_until.isoformat(),
            )
            return
        for position, tick in enumerate(ticks):
            if position < len(ticks) - 1:
                try:
                    replayed = await fire_trigger(
                        trigger_id=engine_lease.entity_id,
                        scheduled_for=tick,
                        deps=deps,
                    )
                    failed_deliveries += _failed_deliveries(replayed)
                except Exception:
                    replay_errors += 1
                    logger.exception(
                        "trigger %s: catchup fire at %s raised; "
                        "skipping to next",
                        engine_lease.entity_id, tick.isoformat() if tick is not None else None,
                    )
                continue
            current = await fire_trigger(
                trigger_id=engine_lease.entity_id,
                scheduled_for=tick,
                deps=deps,
            )
            failed_deliveries += _failed_deliveries(current)
        if failed_deliveries or replay_errors:
            # Honest claim-row bookkeeping only: TriggerClaimAdapter
            # advances next_fire_at whatever the outcome, so this does
            # not retry anything. The findable record of each failed
            # delivery is the trigger.delivery_failed event plus the
            # subscription's own last_fire_error (primer/trigger/
            # dispatch.py); this just stops the release reporting a
            # clean run over them.
            last_error = (
                f"{failed_deliveries} subscription delivery(ies) failed, "
                f"{replay_errors} catchup fire(s) raised"
            )
            logger.warning(
                "trigger %s: %s (see trigger.delivery_failed events)",
                engine_lease.entity_id, last_error,
            )
        else:
            success = True
    except Exception:
        logger.exception(
            "engine trigger fire for %s raised",
            engine_lease.entity_id,
        )
    finally:
        if early_until is not None:
            outcome = ReleaseOutcome(
                success=True, entity_noop=True, requeue_after=max(early_until - now, timedelta(0)),
            )
        else:
            drop, requeue_after = await _lease_after_the_fire(pool, engine_lease.entity_id)
            outcome = ReleaseOutcome(
                success=success, drop_lease=drop, requeue_after=requeue_after, last_error=last_error,
            )
        await pool._release_lease(engine_lease, outcome)
