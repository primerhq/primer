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


async def run_engine_trigger(pool: "WorkerPool", engine_lease: "ClaimLease") -> None:
    """Handle a TRIGGER claim from the engine.

    Routes the lease to :func:`primer.trigger.dispatch.fire_trigger`,
    which fans out to each enabled subscription's dispatcher. The
    ``TriggerClaimAdapter.on_release`` hook advances ``next_fire_at``
    (cron tick for ``scheduled``, null/disabled for ``delayed``) so
    the engine's next claim window is correct.

    Catchup handling (spec §8): when the trigger's ``catchup`` is
    ``'all'`` and the row has a ``last_fired_at``, enumerate every
    missed cron tick between then and now (bounded to 64 to avoid
    runaway) and fire each one with the historical ``scheduled_for``
    instant. After replaying the backlog we fire the current tick
    with ``scheduled_for=None``. ``'one'`` and ``'none'`` (and all
    non-scheduled kinds) fire exactly once with ``scheduled_for=None``.
    """
    from datetime import datetime, timezone

    from primer.int.claim import ReleaseOutcome
    from primer.model.trigger import Trigger
    from primer.trigger.cron import iter_missed_fires
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
    try:
        # Catchup replay for scheduled triggers with catchup='all'.
        # Best-effort: any failure in the backlog walk falls through
        # to the current-tick fire so a malformed cron / tz doesn't
        # silently block normal firing. The current tick's own
        # errors are still raised to the outer except.
        triggers_storage = pool._storage.get_storage(Trigger)
        trigger = await triggers_storage.get(engine_lease.entity_id)
        if (
            trigger is not None
            and trigger.enabled
            and trigger.config.kind == "scheduled"
            and getattr(trigger.config, "catchup", "one") == "all"
            and trigger.last_fired_at is not None
        ):
            now = datetime.now(timezone.utc)
            try:
                missed = list(iter_missed_fires(
                    trigger.config.cron,
                    trigger.config.timezone,
                    from_=trigger.last_fired_at,
                    now=now,
                    limit=64,
                ))
            except Exception:
                logger.exception(
                    "trigger %s: catchup enumeration failed; "
                    "continuing to current-tick fire",
                    engine_lease.entity_id,
                )
                missed = []
            for missed_ts in missed:
                try:
                    replayed = await fire_trigger(
                        trigger_id=engine_lease.entity_id,
                        scheduled_for=missed_ts,
                        deps=deps,
                    )
                    failed_deliveries += _failed_deliveries(replayed)
                except Exception:
                    replay_errors += 1
                    logger.exception(
                        "trigger %s: catchup fire at %s raised; "
                        "skipping to next",
                        engine_lease.entity_id, missed_ts.isoformat(),
                    )

        current = await fire_trigger(
            trigger_id=engine_lease.entity_id,
            scheduled_for=None,
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
        await pool._release_lease(
            engine_lease,
            ReleaseOutcome(
                success=success, drop_lease=False, last_error=last_error,
            ),
        )
