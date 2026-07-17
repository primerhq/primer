"""Pure, self-contained lifespan phases extracted from ``_app_lifespan``.

Each function here was a straight-line block inside the ``_lifespan`` closure
in :mod:`primer.api._app_lifespan`. They are "pure" in the decomposition sense:
each owns no teardown handle and no construct-then-backfill seam, so
``_lifespan`` can call it inline exactly where the block used to live, passing
explicit dependencies instead of closing over them. Behaviour is preserved
verbatim — only the lexical home of the code changed.

See ``_app_bootstrap.py`` for the precedent (the same style of extraction
applied during the earlier app.py split).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from primer.api.config import AppConfig


logger = logging.getLogger(__name__)


async def run_first_boot_bootstrap(
    config: AppConfig, storage_provider
) -> None:
    """First-boot auto-bootstrap.

    Run synchronously before serving so the reserved-id providers are
    available by the time any request arrives. Cost <2s on warm disk (models
    download lazily, not here).
    """
    if config.auto_bootstrap:
        from primer.bootstrap.runner import BootstrapRunner
        from primer.model.provider import (
            CrossEncoderProvider,
            EmbeddingProvider,
            SemanticSearchProvider as _SSP,
        )
        from primer.model.workspace import WorkspaceProvider, WorkspaceTemplate
        _runner = BootstrapRunner(
            storage=storage_provider,
            embedder_storage=storage_provider.get_storage(EmbeddingProvider),
            ssp_storage=storage_provider.get_storage(_SSP),
            cross_encoder_storage=storage_provider.get_storage(
                CrossEncoderProvider
            ),
            workspace_provider_storage=storage_provider.get_storage(
                WorkspaceProvider
            ),
            workspace_template_storage=storage_provider.get_storage(
                WorkspaceTemplate
            ),
            root_dir=Path("~/.primer").expanduser(),
        )
        if await _runner.needs_bootstrap():
            logger.info("first boot detected; running auto-bootstrap")
            _result = await _runner.run()
            logger.info(
                "auto-bootstrap complete",
                extra={
                    "bootstrap_created": _result.created,
                    "bootstrap_skipped": _result.skipped,
                    "error_count": len(_result.errors),
                },
            )
            if _result.errors:
                logger.warning(
                    "auto-bootstrap partial failure",
                    extra={"bootstrap_errors": _result.errors},
                )
    else:
        # Warn on first boot only (marker still null).
        _state = await storage_provider.get_system_state()
        if _state.bootstrap_completed_at is None:
            logger.warning(
                "first boot detected; auto_bootstrap=False — "
                "manual provisioning required"
            )


async def seed_default_artifact_provider(asp_storage) -> None:
    """Seed the reserved default DB-backed artifact provider.

    Idempotent: a concurrent boot may race the create.
    """
    from primer.api.registries.artifact_storage_registry import (
        DEFAULT_ARTIFACT_PROVIDER_ID,
    )
    from primer.model.provider import ArtifactStorageProvider
    try:
        if await asp_storage.get(DEFAULT_ARTIFACT_PROVIDER_ID) is None:
            await asp_storage.create(ArtifactStorageProvider(
                id=DEFAULT_ARTIFACT_PROVIDER_ID, provider="db",
            ))
            logger.info(
                "bootstrap: created reserved default artifact provider (db)",
            )
    except Exception:
        logger.exception("seeding default artifact provider failed")


async def recover_sessions(claim_engine, scheduler, storage_provider) -> None:
    """Session recovery on startup.

    The claim engine + scheduler are in-memory; their state does NOT survive a
    process restart. Persisted WorkspaceSession rows DO. Scan for non-ENDED
    rows and re-arm the engine so workers can claim them again. Without this, a
    session created in the previous process sits at status=RUNNING forever with
    no owner — the diagnostic-report Bug 1.
    """
    try:
        from primer.int.claim import ClaimKind as _ClaimKind
        from primer.model.storage import OffsetPage as _OffsetPage
        from primer.model.workspace_session import (
            SessionStatus as _SessionStatus,
            WorkspaceSession as _WorkspaceSession,
        )
        from primer.storage.q import Q as _Q

        _session_storage = storage_provider.get_storage(_WorkspaceSession)
        # Only LIVE (non-ENDED) sessions can still need work. Pushing
        # the filter into the query (instead of list()-ing every row
        # and dropping ENDED in Python) keeps recovery from loading the
        # entire session history into memory at scale -- and the new
        # B-tree index on sessions.status keeps the scan cheap. The
        # IN-set mirrors "every status except ENDED" so a future status
        # is recovered (fail-safe) rather than silently dropped.
        _live_statuses = [
            s.value for s in _SessionStatus if s != _SessionStatus.ENDED
        ]
        _live_predicate = (
            _Q(_WorkspaceSession).where_in("status", _live_statuses).build()
        )
        _recovered_running = 0
        _recovered_other = 0
        _offset = 0
        while True:
            _page = await _session_storage.find(
                _live_predicate,
                _OffsetPage(offset=_offset, length=200),
            )
            _items = list(_page.items)
            for _sess in _items:
                if _sess.status == _SessionStatus.ENDED:
                    continue
                try:
                    await claim_engine.upsert(_ClaimKind.SESSION, _sess.id)
                    if _sess.status == _SessionStatus.RUNNING:
                        # Also notify the scheduler — Postgres
                        # enqueue is pg_notify-only; in-memory is
                        # idempotent.
                        try:
                            await scheduler.enqueue(_sess.id)
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "session recovery: scheduler.enqueue "
                                "failed for %s (lease will still be "
                                "claimable)", _sess.id, exc_info=True,
                            )
                        _recovered_running += 1
                    else:
                        _recovered_other += 1
                except Exception:
                    logger.exception(
                        "session recovery: failed to upsert lease "
                        "for %s", _sess.id,
                    )
            if len(_items) < 200:
                break
            _offset += 200
        if _recovered_running or _recovered_other:
            logger.info(
                "lifespan: session recovery — re-armed %d RUNNING + "
                "%d non-RUNNING leases from persisted state",
                _recovered_running, _recovered_other,
            )
    except Exception:  # noqa: BLE001 -- never break startup
        logger.exception("lifespan: session recovery failed")


async def recover_chats(claim_engine, storage_provider) -> None:
    """Chat recovery on startup.

    Same shape as session recovery above but for the chat surface. A chat row
    at turn_status='claimable' or 'running' with no lease (because the worker
    died between writing a chat message and releasing) would otherwise sit
    stuck forever — see bug-2026-06-02T192011Z-8feeba2a. ChatClaimAdapter's
    eligibility predicate requires turn_status in {claimable, running} and
    chat.status='active', so we only re-arm rows that match.
    """
    try:
        from primer.int.claim import ClaimKind as _ClaimKind
        from primer.model.chats import Chat as _Chat
        from primer.model.storage import OffsetPage as _OffsetPage
        from primer.storage.q import Q as _Q

        _chats_storage = storage_provider.get_storage(_Chat)
        # Push the eligibility filter into the query (mirrors session
        # recovery above) instead of list()-ing every chat and dropping
        # non-matching rows in Python: at scale that loads the entire chat
        # history into memory. ChatClaimAdapter only accepts rows with
        # status='active' AND turn_status in {claimable, running}, so we
        # re-arm exactly those. The new chat.(status,turn_status) B-tree
        # index keeps the scan cheap.
        _chat_predicate = (
            _Q(_Chat)
            .where("status", "active")
            .where_in("turn_status", ["claimable", "running"])
            .build()
        )
        _recovered_chats = 0
        _chat_offset = 0
        while True:
            _page = await _chats_storage.find(
                _chat_predicate,
                _OffsetPage(offset=_chat_offset, length=200),
            )
            _items = list(_page.items)
            for _chat in _items:
                try:
                    await claim_engine.upsert(_ClaimKind.CHAT, _chat.id)
                    _recovered_chats += 1
                except Exception:
                    logger.exception(
                        "chat recovery: failed to upsert lease for %s",
                        _chat.id,
                    )
            if len(_items) < 200:
                break
            _chat_offset += 200
        if _recovered_chats:
            logger.info(
                "lifespan: chat recovery — re-armed %d chat lease(s) "
                "from persisted state", _recovered_chats,
            )
    except Exception:  # noqa: BLE001 -- never break startup
        logger.exception("lifespan: chat recovery failed")


# Grace window: only pending rows OLDER than this are re-fired. It is the only
# thing separating a re-fire from a live sibling's in-flight dispatch.
#
# MULTI-PROCESS RE-FIRE HAZARD (known, bounded, NOT eliminated): every API
# process runs this phase on boot and takes no claim or lock on a delivery. On
# a rolling deploy the booting replica re-fires the draining replica's still
# pending rows whenever their dispatch outlives this window - and an
# agent_fresh_session dispatch (workspace + session creation) plausibly exceeds
# the old 30s default. Unlike recover_sessions / recover_chats, whose
# idempotent lease re-arms are harmless to repeat, this phase has real external
# side effects (it spawns chats/sessions), so a spurious re-fire is a duplicate
# delivery rather than a no-op.
#
# A window only NARROWS that race. Closing it means routing recovery through
# the claim machine (primer/claim/) so exactly one process owns each delivery,
# which needs a new ClaimKind + adapter + a consumer loop that heartbeats
# through a long dispatch; that is a follow-up, not a constant. Meanwhile the
# default is raised well clear of a typical dispatch.
#
# THE TRADE (read this before changing the value): the boot sweep is ONE-SHOT.
# A row still inside the grace band when the sweep runs is NOT re-fired by that
# pass, and nothing re-runs the sweep - so such a row is not "bounded by the
# boot cadence": on a stable deployment the next boot may be weeks away, or
# never. To make the bound real, recover_webhook_deliveries schedules a single
# delayed re-check of the ids it grace-skipped, which fires once the youngest
# of them clears the window. Recovery latency for a dropped delivery is
# therefore at most this window (re-check) rather than unbounded (next boot),
# and the value is a straight duplicate-suppression-vs-recovery-latency trade:
# larger suppresses more spurious re-fires of a live sibling's slow dispatch
# and defers a genuinely dropped delivery by up to that long. Raise it on
# deployments with slow fresh-session dispatches or long rolling-deploy drains;
# if the re-check task is lost (process exits before it fires) the row falls
# back to the next boot's sweep.
_WEBHOOK_DELIVERY_GRACE_SECS_DEFAULT = 300.0


def _read_webhook_grace_secs() -> float:
    """Parse the grace override, falling back to the default.

    This runs at IMPORT time, so an unparseable value must not raise: a
    typo'd PRIMER_WEBHOOK_RECOVERY_GRACE_SECS would otherwise propagate out
    of the import and take the whole API down at boot. A bad override is an
    operator mistake worth shouting about, not a reason to refuse to start.
    """
    _raw = os.environ.get("PRIMER_WEBHOOK_RECOVERY_GRACE_SECS")
    if _raw is None:
        return _WEBHOOK_DELIVERY_GRACE_SECS_DEFAULT
    try:
        return float(_raw)
    except (TypeError, ValueError):
        logger.warning(
            "PRIMER_WEBHOOK_RECOVERY_GRACE_SECS=%r is not a number; "
            "falling back to the %.0fs default",
            _raw, _WEBHOOK_DELIVERY_GRACE_SECS_DEFAULT,
        )
        return _WEBHOOK_DELIVERY_GRACE_SECS_DEFAULT


_WEBHOOK_DELIVERY_GRACE_SECS = _read_webhook_grace_secs()

# Page size for the recovery sweep's scan of pending rows. Bounded by the
# OffsetPage contract (length is 1..200).
_WEBHOOK_RECOVERY_PAGE_SIZE = 200

# Poison-pill cap: give up on a delivery after this many dispatch attempts.
# WebhookDelivery.attempts counts attempts STARTED (the endpoint records its
# own BackgroundTask as attempt 1; the sweep below bumps the count BEFORE
# re-dispatching), so a row whose dispatch hard-crashes the process, or whose
# done/failed marking keeps failing, still advances the counter. Without the
# cap such a row is re-fired on EVERY subsequent boot forever, each time
# spawning duplicate chats/sessions. 3 leaves room for two crash recoveries.
_WEBHOOK_DELIVERY_MAX_ATTEMPTS = 3


async def _dispatch_recovered_deliveries(
    _ids: list[str],
    _storage,
    _cutoff: float,
    storage_provider,
    event_bus,
    claim_engine,
    scheduler,
    workspace_registry,
) -> int:
    """Re-read each collected id and dispatch the ones still eligible.

    Takes ids rather than rows on purpose. The sweep that collects them can
    span an unbounded number of pending rows, and each row carries its
    ``extra_context.webhook_body`` (up to the endpoint's 1 MB cap), so
    holding the whole collected set as rows is a boot-path OOM in exactly
    the mass-crash case recovery exists for. Ids bound that to strings.

    Re-reading also makes every check act on FRESH state: a live sibling may
    have finalized a row between collection and dispatch, and the re-read
    row's ``status`` / ``attempts`` reflect that, so the sweep skips it
    instead of re-firing a delivery someone else already made.

    Returns the number of deliveries actually re-dispatched.
    """
    from datetime import datetime, timezone as _tz

    from primer.api.routers.webhooks import _dispatch_webhook

    _refired = 0
    for _id in _ids:
        try:
            _row = await _storage.get(_id)
        except Exception:
            logger.exception(
                "webhook recovery: could not re-read delivery %s; "
                "skipping it this sweep", _id,
            )
            continue
        # Gone, or finalized by a live sibling between collection and now.
        # Either way it is not ours to re-fire.
        if _row is None or _row.status != "pending":
            continue
        # Re-check the grace window against the fresh row: a row that was
        # stale at collection time is still stale now, but the re-check pass
        # below reuses this helper with a recomputed cutoff.
        if _row.created_at.timestamp() > _cutoff:
            continue
        # Poison-pill cap: a row that keeps coming back (its dispatch
        # kills the process, or _finalize_delivery's update keeps failing
        # and is swallowed) must not be re-fired forever. Give up loudly.
        if _row.attempts >= _WEBHOOK_DELIVERY_MAX_ATTEMPTS:
            logger.error(
                "webhook recovery: delivery %s exhausted %d attempts; "
                "marking failed and giving up",
                _row.id, _row.attempts,
            )
            try:
                await _storage.update(_row.model_copy(update={
                    "status": "failed",
                    "completed_at": datetime.now(_tz.utc),
                }))
            except Exception:
                logger.exception(
                    "webhook recovery: could not mark exhausted delivery "
                    "%s failed", _row.id,
                )
            continue
        # Record the attempt BEFORE dispatching. Counting it afterwards
        # would never count the cases the cap exists for: a dispatch that
        # hard-crashes the process, or a finalize that keeps failing,
        # never reaches a post-hoc increment.
        try:
            _row = await _storage.update(
                _row.model_copy(update={"attempts": _row.attempts + 1})
            )
        except Exception:
            # Cannot account for this attempt, so do not take it: firing
            # uncounted is exactly the unbounded re-fire the cap removes.
            # The row stays pending and a later boot can retry it.
            logger.exception(
                "webhook recovery: could not record an attempt for %s; "
                "skipping it this sweep", _row.id,
            )
            continue
        try:
            await _dispatch_webhook(
                _row.trigger_id,
                _row.extra_context,
                storage_provider,
                event_bus,
                claim_engine,
                scheduler,
                workspace_registry,
                delivery_id=_row.id,
            )
            _refired += 1
        except Exception:
            logger.exception(
                "webhook recovery: re-dispatch failed for %s", _row.id,
            )
    return _refired


async def _recheck_grace_skipped_deliveries(
    _ids: list[str],
    _delay: float,
    _storage,
    storage_provider,
    event_bus,
    claim_engine,
    scheduler,
    workspace_registry,
) -> None:
    """Re-check deliveries the boot sweep skipped for the grace window.

    The sweep is one-shot, so without this a row younger than the cutoff at
    boot is never revisited by this process and waits for the NEXT boot -
    which on a stable deployment may never come. Sleeping until the youngest
    skipped row clears the window turns the grace into a genuine upper bound
    on recovery latency.

    One sleep and one pass, not a poll: every id is past the window by the
    time this runs, so there is nothing left to wait for afterwards. The task
    is owned and cancelled by the lifespan, so it cannot outlive the app.
    """
    from datetime import datetime, timezone as _tz

    try:
        await asyncio.sleep(_delay)
        # Recompute against the clock we woke on, not the sweep's cutoff.
        _cutoff = (
            datetime.now(_tz.utc).timestamp() - _WEBHOOK_DELIVERY_GRACE_SECS
        )
        _refired = await _dispatch_recovered_deliveries(
            _ids,
            _storage,
            _cutoff,
            storage_provider,
            event_bus,
            claim_engine,
            scheduler,
            workspace_registry,
        )
        if _refired:
            logger.info(
                "lifespan: webhook recovery - re-dispatched %d delivery(ies) "
                "that were inside the grace window at boot", _refired,
            )
    except asyncio.CancelledError:
        # Shutdown. Anything still pending falls to the next boot's sweep.
        raise
    except Exception:  # noqa: BLE001 -- a background task must not die loudly
        logger.exception("lifespan: webhook grace re-check failed")


async def recover_webhook_deliveries(
    storage_provider,
    event_bus,
    claim_engine,
    scheduler,
    workspace_registry,
) -> asyncio.Task | None:
    """Re-dispatch inbound webhook deliveries the previous process dropped.

    The webhook endpoint persists a ``WebhookDelivery`` row (status
    ``pending``) BEFORE returning 202 and BEFORE its in-process
    ``BackgroundTask`` dispatches the trigger. If the process died between
    the 202 and dispatch completion the row stays ``pending`` forever and
    the delivery is lost (senders never retry a 202). Scan for ``pending``
    rows older than a small grace window and re-run the SAME
    ``_dispatch_webhook`` path.

    Idempotency: the ``fire_id`` gate in ``fire_trigger`` does NOT dedupe a
    webhook re-fire - ``fire_trigger`` recomputes a fresh wall-clock
    ``fired_at`` for every webhook call (``scheduled_for=None``), so each
    dispatch derives a different ``fire_id`` and ``last_fired_id`` never
    matches. The durable ``WebhookDelivery.status`` IS the dedupe: only
    ``pending`` rows are re-fired and ``_dispatch_webhook`` flips the row to
    ``done``/``failed``, so a ``done`` row is never re-dispatched. Delivery
    is therefore at-least-once (a crash in the delivered-but-not-yet-marked
    window can double-deliver), matching the endpoint's best-effort marking.

    A row that survives repeated re-fires (its dispatch keeps killing the
    process, or its done/failed marking keeps failing) is abandoned at
    ``_WEBHOOK_DELIVERY_MAX_ATTEMPTS`` and marked ``failed`` rather than
    re-fired on every boot forever.

    Rows still inside the grace window when the sweep runs are not re-fired
    by this one-shot pass. Returns a single ``asyncio.Task`` that re-checks
    exactly those ids once they clear the window (``None`` when there are
    none). The caller OWNS that task and must cancel it on shutdown, so the
    grace is a real bound on recovery latency rather than a deferral to the
    next boot.
    """
    try:
        from datetime import datetime, timezone as _tz

        from primer.model.storage import OffsetPage as _OffsetPage
        from primer.model.webhook_delivery import WebhookDelivery
        from primer.storage.q import Q as _Q

        _storage = storage_provider.get_storage(WebhookDelivery)
        # status is filtered in SQL; the created_at grace is applied in
        # Python because a datetime is not a JSON-scalar predicate value.
        _pending_predicate = (
            _Q(WebhookDelivery).where("status", "pending").build()
        )
        _cutoff = datetime.now(_tz.utc).timestamp() - _WEBHOOK_DELIVERY_GRACE_SECS

        # Collect the whole stale set BEFORE dispatching any of it, as IDS.
        # _dispatch_webhook flips its row 'pending' -> 'done'/'failed', which
        # MUTATES the very predicate this query pages over. Dispatching inside
        # the paging loop therefore shrinks the result set under the cursor and
        # silently skips rows: with 500 stale rows, offset=0 drains 1-200 and
        # marks them done, leaving 201-500 as the pending set, so the next read
        # at offset=200 returns 401-500 and rows 201-400 are never dispatched
        # (they stay pending until some future boot) - worst exactly in the
        # mass-crash case this phase exists for. Collecting first keeps the
        # paging stable over an unchanging set, and is simpler to reason about
        # than re-querying offset=0 until drained while tracking grace-skipped
        # ids to keep a fresh row from looping forever.
        #
        # Ids, not rows: the pending set is unbounded (rows leave 'pending'
        # only via dispatch and the table is never pruned) and every row
        # carries its webhook_body, so holding the collected rows is a boot
        # OOM in the mass-crash case. _dispatch_recovered_deliveries re-reads
        # each id right before dispatching it.
        _stale_ids: list[str] = []
        # Ids skipped for the grace window, plus the wall-clock instant the
        # youngest of them clears it. The re-check task below waits for that
        # instant, so one pass suffices for all of them.
        _grace_ids: list[str] = []
        _grace_deadline = 0.0
        _offset = 0
        while True:
            _page = await _storage.find(
                _pending_predicate,
                _OffsetPage(offset=_offset, length=_WEBHOOK_RECOVERY_PAGE_SIZE),
            )
            _items = list(_page.items)
            for _row in _items:
                _created = _row.created_at.timestamp()
                if _created <= _cutoff:
                    _stale_ids.append(_row.id)
                else:
                    # Younger than the grace window - it may be a live
                    # process's in-flight dispatch, so leave it to the
                    # re-check rather than racing it now.
                    _grace_ids.append(_row.id)
                    _grace_deadline = max(
                        _grace_deadline,
                        _created + _WEBHOOK_DELIVERY_GRACE_SECS,
                    )
            # Advance on the UNFILTERED page length: the grace filter above is
            # applied in Python, so a page can be wholly grace-skipped while
            # more pages remain. Terminating on the filtered count would end
            # the sweep early; paging on len(_items) is also what makes this
            # loop unable to run forever (a short page always ends it).
            if len(_items) < _WEBHOOK_RECOVERY_PAGE_SIZE:
                break
            _offset += _WEBHOOK_RECOVERY_PAGE_SIZE

        _refired = await _dispatch_recovered_deliveries(
            _stale_ids,
            _storage,
            _cutoff,
            storage_provider,
            event_bus,
            claim_engine,
            scheduler,
            workspace_registry,
        )
        if _refired:
            logger.info(
                "lifespan: webhook recovery - re-dispatched %d stale "
                "pending delivery(ies) from persisted state", _refired,
            )

        if not _grace_ids:
            return None
        # Wait only until the YOUNGEST grace-skipped row clears the window;
        # every older one has cleared it by then, so a single pass drains the
        # set and the task ends. Clamped to the window: a row dated in the
        # future (clock skew) must not stretch the wait past it, and a
        # deadline already in the past means fire straight away.
        _delay = min(
            max(_grace_deadline - datetime.now(_tz.utc).timestamp(), 0.0),
            _WEBHOOK_DELIVERY_GRACE_SECS,
        )
        logger.info(
            "lifespan: webhook recovery - %d pending delivery(ies) are inside "
            "the %.0fs grace window; re-checking them in %.0fs",
            len(_grace_ids), _WEBHOOK_DELIVERY_GRACE_SECS, _delay,
        )
        return asyncio.create_task(
            _recheck_grace_skipped_deliveries(
                _grace_ids,
                _delay,
                _storage,
                storage_provider,
                event_bus,
                claim_engine,
                scheduler,
                workspace_registry,
            ),
            name="webhook-recovery-grace-recheck",
        )
    except Exception:  # noqa: BLE001 -- never break startup
        logger.exception("lifespan: webhook recovery failed")
        return None


async def recover_ic_bootstrap(storage_provider) -> None:
    """Internal-collections bootstrap recovery.

    If a bootstrap was in flight when the previous API process exited, its
    asyncio task is gone but the status row still says "running". Mark it as
    failed so the UI surfaces the interruption and the operator can re-trigger.
    """
    from primer.model.internal import (
        INTERNAL_COLLECTIONS_BOOTSTRAP_STATUS_ID,
        InternalCollectionsBootstrapStatus,
    )
    from datetime import datetime, timezone as _tz
    _status_storage = storage_provider.get_storage(
        InternalCollectionsBootstrapStatus
    )
    _stale = await _status_storage.get(
        INTERNAL_COLLECTIONS_BOOTSTRAP_STATUS_ID
    )
    if _stale is not None and _stale.status == "running":
        logger.warning(
            "ic bootstrap recovery: marking stale 'running' row as "
            "failed (attempt_id=%s)", _stale.attempt_id,
        )
        await _status_storage.update(_stale.model_copy(update={
            "status": "failed",
            "phase": None,
            "finished_at": datetime.now(_tz.utc),
            "error": (
                "bootstrap was interrupted by an API process "
                "restart; re-trigger when ready."
            ),
        }))


def assert_harness_kinds_registered() -> None:
    """Startup invariant: every kind the harness service manages must appear in
    the CDC kinds registry.

    ``_harness_kind_models()`` ensures the registry is fully populated (handles
    test-reset and lazy-import cases), then we assert no required kind is
    missing. Note: EntityType (agent/graph/collection/tool) intentionally omits
    "document" and "toolset" (no IC vector index for those), so we check
    harness-managed storage kinds rather than EntityType.
    """
    from primer.harness.service import _harness_kind_models  # noqa: PLC0415
    _required_harness_kinds = frozenset(
        {"agent", "graph", "collection", "document", "toolset"}
    )
    _registered = frozenset(_harness_kind_models().keys())
    _missing = _required_harness_kinds - _registered
    assert not _missing, (
        f"CDC kinds registry is missing harness-managed kinds: {_missing!r}. "
        "Ensure the corresponding router modules register their kinds."
    )


async def warm_chat_channels(channel_registry) -> None:
    """Warm the enabled chat-channel adapters at startup.

    Session channels start on the first outbound park, but a chat is
    user-initiated and has no other start trigger, so warm the enabled
    chat-channel adapters. Only the inbound-owning process calls this: warming
    opens an inbound listener, and a worker-only process must not open a second
    competing inbound connection.
    """
    try:
        warmed = await channel_registry.warm_chat_channels()
        if warmed:
            logger.info("warmed %d chat-channel adapter(s)", warmed)
    except Exception:
        logger.exception("warm_chat_channels failed during startup")


async def sample_claim_queue_depth(claim_engine) -> None:
    """Observability loop: sample the claim queue depth every 10s.

    Only meaningful for a Postgres-backed claim engine (the in-memory engine's
    depth would always be 0 outside tests); the caller gates on that before
    scheduling this loop.
    """
    import primer.observability.metrics as _m
    _table = claim_engine._table  # noqa: SLF001
    _pool = claim_engine._storage.pool  # noqa: SLF001
    while True:
        try:
            await asyncio.sleep(10)
            async with _pool.acquire() as _conn:
                _rows = await _conn.fetch(
                    f"SELECT kind, COUNT(*) AS cnt"
                    f" FROM {_table}"
                    f" WHERE claimed_by IS NULL"
                    f" GROUP BY kind"
                )
            for _row in _rows:
                _m.claim_queue_depth.labels(_row["kind"]).set(
                    _row["cnt"]
                )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.debug(
                "claim queue-depth sample failed", exc_info=True
            )


async def forward_chat_ticks(event_bus, chat_tick_router) -> None:
    """Forward ``chat:<id>:tick`` bus events into the process-local tick router.

    One bus subscription per process feeds the router; WS handlers subscribe
    per-chat off the router.
    """
    from primer.chat.tick_router import Tick

    sub = event_bus.subscribe()
    try:
        async for event in sub:
            key = event.event_key
            if not key.startswith("chat:") or not key.endswith(":tick"):
                continue
            cid = key[len("chat:"):-len(":tick")]
            if not cid:
                continue
            seq = event.payload.get("seq") if event.payload else None
            if not isinstance(seq, int):
                continue
            chat_tick_router.publish(cid, Tick(seq=seq))
    except asyncio.CancelledError:
        pass
    finally:
        await sub.aclose()


async def forward_chat_relays(
    event_bus, storage_provider, channel_registry, artifact_storage_registry
) -> None:
    """Chat -> channel relay forwarder.

    An out-of-proc worker cannot post to a channel (it deliberately does not
    own the inbound gateway), so it publishes a tiny ``chat:<id>:relay`` signal;
    the inbound-owning process re-derives the text/gate from storage and posts
    via its warm adapter. Only runs where inbound lives (API / api+worker). In a
    single api+worker process the worker posts directly via the shared warm
    registry and never publishes, so this stays idle there.
    """
    from primer.channel.chat_dispatcher import (
        ChatChannelDispatcher,
        derive_chat_gate_envelope,
        derive_final_relay_media,
        derive_final_relay_text,
        parse_relay_event_key,
    )

    relayer = ChatChannelDispatcher(
        storage_provider=storage_provider,
        registry=channel_registry,
        event_bus=None,  # never republish: terminal, no bus loop
        allow_build=True,  # inbound-owning: may warm the adapter
        artifact_registry=artifact_storage_registry,
    )
    sub = event_bus.subscribe()
    try:
        async for event in sub:
            cid = parse_relay_event_key(event.event_key)
            if cid is None:
                continue
            kind = (event.payload or {}).get("kind")
            try:
                if kind == "text":
                    text = await derive_final_relay_text(
                        storage_provider, cid)
                    if text:
                        await relayer.relay_text(chat_id=cid, text=text)
                elif kind == "gate":
                    env = await derive_chat_gate_envelope(
                        storage_provider, cid)
                    if env is not None:
                        await relayer.dispatch_gate(
                            chat_id=cid, envelope=env)
                elif kind == "media":
                    mparts = await derive_final_relay_media(
                        storage_provider, cid)
                    if mparts:
                        await relayer.relay_media(
                            chat_id=cid, parts=mparts)
            except Exception:
                logger.exception(
                    "chat relay forwarder: post for %s failed", cid)
    except asyncio.CancelledError:
        pass
    finally:
        await sub.aclose()
