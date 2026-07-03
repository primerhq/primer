"""Per-kind engine claim handlers for the worker pool.

Extracted verbatim from :mod:`primer.worker.pool` (no behaviour change). One
function per non-session claim kind (chat / harness / trigger); each takes the
:class:`~primer.worker.pool.WorkerPool` instance as ``pool`` and reads the same
bound deps the original methods did (``pool._storage`` / ``pool._engine`` /
``pool._event_bus`` / ...). The pool keeps thin delegating methods
(``WorkerPool._run_engine_chat`` etc.) so the ``start()`` dispatch table
(``self._run_engine_chat``) and any test monkeypatches still resolve through
the instance.

The SESSION handler (``_run_engine_session``) intentionally stays in
``pool.py``: it is the ``run_one_session_turn`` monkeypatch seam
(``patch("primer.worker.pool.run_one_session_turn")``).

Per-kind dispatch imports stay lazy inside each function so importing this
module (and ``pool``) doesn't pull the chat / harness / trigger dependency
trees at startup — mirroring the original methods.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primer.int.claim import Lease as ClaimLease
    from primer.worker.pool import WorkerPool

logger = logging.getLogger(__name__)


async def run_engine_chat(pool: "WorkerPool", engine_lease: "ClaimLease") -> None:
    """Handle a CHAT claim from the engine.

    Bridges to run_one_chat_turn via ChatDispatchDeps.
    Atomically stamps claimed_by + turn_status='running' on the chat row
    before dispatching, then releases the engine lease on completion.
    """
    from primer.chat.dispatch import ChatDispatchDeps, run_one_chat_turn
    from primer.int.claim import ReleaseOutcome
    from primer.model.chats import Chat

    assert pool._event_bus is not None, (
        "WorkerPool._run_engine_chat requires an event_bus"
    )
    assert pool._chat_tick_router is not None, (
        "WorkerPool._run_engine_chat requires a chat_tick_router"
    )

    # Transition turn_status to 'running' before dispatching. 'running'
    # is accepted here for crash recovery: the claim engine only hands us
    # a 'running' chat when its prior worker's lease expired (died/stalled
    # past the TTL), so we re-run the interrupted turn. Fencing
    # (attempt_id / lease loss) stops the dead worker from double-writing.
    chat_storage = pool._storage.get_storage(Chat)
    chat = await chat_storage.get(engine_lease.entity_id)
    if chat is None or chat.turn_status not in (
        "claimable", "running",
    ):
        await pool._engine.release(
            engine_lease,
            outcome=ReleaseOutcome(success=False, drop_lease=True),
        )
        return
    await chat_storage.update(chat.model_copy(update={
        "turn_status": "running",
    }))

    chat_channel_dispatcher = None
    if pool._channel_dispatcher is not None:
        from primer.channel.chat_dispatcher import ChatChannelDispatcher
        chat_channel_dispatcher = ChatChannelDispatcher(
            storage_provider=pool._storage,
            registry=pool._channel_dispatcher._registry,
            # Out-of-proc workers never warm inbound gateways, so relay
            # peeks the (cold) registry and falls back to the bus; the
            # API process fulfils the post. In-proc (api+worker) the
            # shared registry is warm and relay posts directly.
            event_bus=pool._event_bus,
            artifact_registry=pool._artifact_storage_registry,
        )

    deps = ChatDispatchDeps(
        storage_provider=pool._storage,
        provider_registry=pool._provider_registry,
        event_bus=pool._event_bus,
        chat_tick_router=pool._chat_tick_router,
        chat_channel_dispatcher=chat_channel_dispatcher,
        artifact_storage_registry=pool._artifact_storage_registry,
    )
    # run_one_chat_turn returns the terminal turn_status DISPOSITION;
    # it no longer writes turn_status itself. We map it to the
    # ReleaseOutcome the fenced ChatClaimAdapter.on_release turns into
    # the terminal turn_status (the single writer). Adapter rule:
    # idle iff (success and drop_lease). So 'idle' -> drop the lease
    # (idle); 'claimable' -> keep success but DON'T drop the lease so
    # the adapter computes 'claimable' (re-served). A raised turn is
    # treated as 'claimable' too.
    disposition = "claimable"
    try:
        disposition = await run_one_chat_turn(
            deps,
            chat_id=engine_lease.entity_id,
            worker_id=pool._worker_id,
        )
    except Exception:
        logger.exception(
            "engine chat turn for %s raised",
            engine_lease.entity_id,
        )
    finally:
        drop_lease = disposition == "idle"
        await pool._engine.release(
            engine_lease,
            outcome=ReleaseOutcome(success=True, drop_lease=drop_lease),
        )


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
        await pool._engine.release(
            engine_lease,
            outcome=ReleaseOutcome(success=False, drop_lease=True),
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
        await pool._engine.release(
            engine_lease,
            outcome=ReleaseOutcome(success=success, drop_lease=True),
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

    success = False
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
                    await fire_trigger(
                        trigger_id=engine_lease.entity_id,
                        scheduled_for=missed_ts,
                        deps=deps,
                    )
                except Exception:
                    logger.exception(
                        "trigger %s: catchup fire at %s raised; "
                        "skipping to next",
                        engine_lease.entity_id, missed_ts.isoformat(),
                    )

        await fire_trigger(
            trigger_id=engine_lease.entity_id,
            scheduled_for=None,
            deps=deps,
        )
        success = True
    except Exception:
        logger.exception(
            "engine trigger fire for %s raised",
            engine_lease.entity_id,
        )
    finally:
        await pool._engine.release(
            engine_lease,
            outcome=ReleaseOutcome(success=success, drop_lease=False),
        )
