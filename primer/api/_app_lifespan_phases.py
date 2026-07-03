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

        _chats_storage = storage_provider.get_storage(_Chat)
        _recovered_chats = 0
        _chat_offset = 0
        while True:
            _page = await _chats_storage.list(
                _OffsetPage(offset=_chat_offset, length=200)
            )
            _items = list(_page.items)
            for _chat in _items:
                # Skip anything the adapter wouldn't accept.
                if getattr(_chat, "status", None) != "active":
                    continue
                _ts = getattr(_chat, "turn_status", None)
                if _ts not in ("claimable", "running"):
                    continue
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
