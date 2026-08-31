"""Production lifespan handler (startup + shutdown wiring).

Extracted verbatim from :mod:`primer.api.app` as part of the app.py
decomposition. ``_make_lifespan`` builds the full provider / scheduler /
worker / channel / recovery wiring on startup and tears it down on
shutdown. Startup ordering is load-bearing and is preserved exactly as
it was inline in app.py. Re-exported from ``primer.api.app``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid as _uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from primer.api._app_bootstrap import (
    _bootstrap_web_fetch,
    _bootstrap_web_search,
)
from primer.api._app_lifespan_phases import (
    assert_harness_kinds_registered,
    recover_ic_bootstrap,
    recover_sessions,
    recover_webhook_deliveries,
    run_first_boot_bootstrap,
    sample_claim_queue_depth,
    seed_default_artifact_provider,
    warm_chat_channels,
)
from primer.api._app_mcp import _start_mcp_mount
from primer.api.config import AppConfig
from primer.api.registries import (
    ProviderRegistry,
    SemanticSearchRegistry,
    WorkspaceRegistry,
)
from primer.api.version import APP_VERSION
from primer.internal_collections import build_subsystem, load_config_or_none
from primer.model.provider import SemanticSearchProvider
from primer.model.scheduler import RuntimeMode, SchedulerProviderType
from primer.toolset.harness import build_harness_toolset_provider
from primer.toolset.misc import build_misc_toolset
from primer.toolset.system import build_system_toolset
from primer.toolset.trigger import build_trigger_toolset_provider
from primer.toolset.web import build_web_toolset
from primer.toolset.crud import build_crud_toolset
from primer.toolset.workspace_ext import build_workspace_ext_toolset
from primer.toolset.workspaces import build_workspaces_toolset
from primer.workspace.probe import WorkspaceProbeTask


logger = logging.getLogger(__name__)


def _make_lifespan(config: AppConfig):
    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        # --- Observability wiring ----------------------------------------
        # Set up OTEL tracing + auto-instrumentation as early as possible
        # in the lifespan so any span-emitting code below is covered.
        from primer.observability import tracing as _tracing
        _tracing.setup(config.observability)

        if config.observability.enabled:
            from primer.observability import logging_integration as _log_integration
            _log_integration.install_log_correlation()

        # When scheduler is unset, default to an in-memory scheduler
        # so the zero-config path boots. Operators running worker mode
        # in production should explicitly set a Postgres scheduler.
        scheduler_config = config.scheduler
        if scheduler_config is None and config.runtime_mode in (
            RuntimeMode.WORKER, RuntimeMode.API_PLUS_WORKER,
        ):
            from primer.model.scheduler import (
                InMemorySchedulerConfig,
                SchedulerProviderConfig as _SchedulerProviderConfig,
                SchedulerProviderType as _SchedulerProviderType,
            )
            scheduler_config = _SchedulerProviderConfig(
                provider=_SchedulerProviderType.IN_MEMORY,
                config=InMemorySchedulerConfig(),
            )

        # Resolve _build_storage_provider through the primer.api.app facade
        # at call time (rather than binding the name at import) so tests that
        # monkeypatch ``primer.api.app._build_storage_provider`` still steer
        # the provider the lifespan builds. This preserves the original
        # single-module patch seam after the app.py split.
        from primer.api import app as _app_facade
        storage_provider = _app_facade._build_storage_provider(config)
        await storage_provider.initialize()
        await storage_provider.get_content_store().ensure_schema()
        await storage_provider.get_event_store().ensure_schema()
        # Ordered data migrations. Runs BEFORE auto-bootstrap so a migration
        # never has to reason about whether a reserved row exists yet: it
        # only ever transforms rows that were already there. A fresh install
        # baselines straight to LATEST_VERSION instead of running the chain
        # against an empty database.
        from primer.storage.migrations import run_migrations

        _state = await storage_provider.get_system_state()
        await run_migrations(
            storage_provider,
            is_fresh_install=_state.bootstrap_completed_at is None,
        )

        from primer.model.provider import SecretProviderConfig
        from primer.secret.factory import SecretProviderFactory

        secret_provider = SecretProviderFactory.create(
            config.secrets or SecretProviderConfig()
        )
        await secret_provider.initialize()

        # --- First-boot auto-bootstrap -----------------------------------
        # Run synchronously before serving so the reserved-id providers
        # are available by the time any request arrives. Cost <2s on
        # warm disk (models download lazily, not here).
        await run_first_boot_bootstrap(config, storage_provider)

        # Existing-install migration + break-glass: Layer 1 RBAC (Task 2)
        # added User.role, defaulting existing rows to "user". Promote
        # the oldest enabled, password-holding user to admin if no admin
        # exists yet — a no-op on fresh installs (register already makes
        # the first account admin) and on every subsequent boot.
        from primer.auth.bootstrap_admin import ensure_admin_exists

        await ensure_admin_exists(storage_provider)

        semantic_search_registry = SemanticSearchRegistry(
            storage=storage_provider.get_storage(SemanticSearchProvider),
        )
        app.state.semantic_search_registry = semantic_search_registry

        # Artifact storage (chat media bytes). Build the registry and seed the
        # reserved default DB-backed provider so media works with zero operator
        # config. Idempotent: a concurrent boot may race the create.
        from primer.api.registries.artifact_storage_registry import (
            ArtifactStorageRegistry,
        )
        from primer.model.provider import ArtifactStorageProvider
        _asp_storage = storage_provider.get_storage(ArtifactStorageProvider)
        artifact_storage_registry = ArtifactStorageRegistry(
            storage=_asp_storage,
            storage_provider=storage_provider,
        )
        app.state.artifact_storage_registry = artifact_storage_registry
        await seed_default_artifact_provider(_asp_storage)

        from primer.agent.approval import ApprovalResolver
        from primer.model.tool_approval import ToolApprovalPolicy

        approval_resolver = ApprovalResolver(
            storage=storage_provider.get_storage(ToolApprovalPolicy),
        )
        app.state.approval_resolver = approval_resolver

        from primer.api.registries.channel_registry import ChannelRegistry
        from primer.channel.dispatcher import ChannelDispatcher
        from primer.channel.inbox import ChannelInbox
        from primer.model.channel import (
            Channel, ChannelProvider,
        )

        channel_inbox = ChannelInbox(
            event_bus=getattr(app.state, "event_bus", None),
            # 01a0518f: lets handle_response look up a park's OWN stored
            # event_key instead of reconstructing it - required now that
            # a graph park's key can be node-qualified.
            storage_provider=storage_provider,
        )
        channel_registry = ChannelRegistry(
            channel_storage=storage_provider.get_storage(Channel),
            channel_provider_storage=storage_provider.get_storage(ChannelProvider),
            inbox=channel_inbox,
            storage_provider=storage_provider,
            event_bus=getattr(app.state, "event_bus", None),
            artifact_registry=artifact_storage_registry,
        )
        channel_dispatcher = ChannelDispatcher(registry=channel_registry)
        app.state.channel_inbox = channel_inbox
        app.state.channel_registry = channel_registry
        app.state.channel_dispatcher = channel_dispatcher
        # The chat-channel warm task is deferred until AFTER the claim engine is
        # built (~app.state.claim_engine below) so warmed adapters receive it and
        # can wake the worker on inbound chat messages.
        workspace_registry = WorkspaceRegistry(
            storage_provider,
            subprocess_timeout_seconds=config.subprocess_timeout_seconds,
            secret_provider=secret_provider,
        )
        # Bootstrap the system toolset before constructing the
        # ProviderRegistry so the registry can short-circuit
        # ``get_toolset('system')`` to it.
        # Resolve the MCP stdio allowlist from AppConfig and bake it into
        # the toolset factory so every MCP provider built from a row is
        # consistently constrained.
        from primer.api.registries.provider_registry import (
            _build_default_toolset_factory,
        )
        allowlist: frozenset[str] | None = (
            frozenset(config.mcp_stdio_allowed_commands)
            if config.mcp_stdio_allowed_commands is not None
            else None
        )
        provider_registry = ProviderRegistry(
            storage_provider,
            toolset_factory=_build_default_toolset_factory(
                allowed_stdio_commands=allowlist,
            ),
            trace_llm_io=config.observability.trace_llm_io,
        )
        system_toolset = build_system_toolset(
            storage_provider=storage_provider,
            provider_registry=provider_registry,
            semantic_search_registry=semantic_search_registry,
            workspace_registry=workspace_registry,
        )
        provider_registry._system_toolset_provider = system_toolset  # noqa: SLF001
        # NOTE: the always-on _workspaces toolset is built later in this
        # lifespan (after the scheduler/claim_engine/event_bus exist) so its
        # session tools can capture those deps. See the build below.
        # Build the always-on _misc toolset (stateless utilities).
        misc_toolset = build_misc_toolset()
        provider_registry._misc_toolset_provider = misc_toolset  # noqa: SLF001
        from primer.toolset.collections import build_collections_toolset
        collections_toolset = build_collections_toolset(
            storage_provider=storage_provider,
            provider_registry=provider_registry,
            semantic_search_registry=semantic_search_registry,
        )
        provider_registry._collections_toolset_provider = collections_toolset  # noqa: SLF001
        # Bootstrap web-search reserved rows BEFORE building the toolset.
        await _bootstrap_web_search(storage_provider)
        logger.info("bootstrap: web-search rows materialised")
        # Construct the web-search registry + service from the bootstrapped rows.
        from primer.api.registries.web_search_registry import (
            WebSearchRegistry,
            default_web_search_factory,
        )
        from primer.model.web_search import (
            ActiveWebSearchConfig,
            WebSearchProvider,
        )
        from primer.web_search.service import WebSearchService

        web_search_registry = WebSearchRegistry(
            storage=storage_provider.get_storage(WebSearchProvider),
            factory=default_web_search_factory,
        )
        web_search_service = WebSearchService(
            registry=web_search_registry,
            active_config_storage=storage_provider.get_storage(
                ActiveWebSearchConfig,
            ),
        )
        app.state.web_search_registry = web_search_registry
        app.state.web_search_service = web_search_service
        logger.info("lifespan: web-search registry + service constructed")
        await _bootstrap_web_fetch(storage_provider)
        logger.info("bootstrap: web-fetch rows materialised")
        from primer.api.registries.web_fetch_registry import (
            WebFetchRegistry, default_web_fetch_factory,
        )
        from primer.model.web_fetch import ActiveWebFetchConfig, WebFetchProvider
        from primer.web_fetch.service import WebFetchService

        web_fetch_registry = WebFetchRegistry(
            storage=storage_provider.get_storage(WebFetchProvider),
            factory=default_web_fetch_factory,
        )
        web_fetch_service = WebFetchService(
            registry=web_fetch_registry,
            active_config_storage=storage_provider.get_storage(ActiveWebFetchConfig),
        )
        app.state.web_fetch_registry = web_fetch_registry
        app.state.web_fetch_service = web_fetch_service
        from primer.api.registries.speech_registry import (
            SpeechRegistry,
            default_stt_factory,
            default_tts_factory,
        )
        from primer.model.provider import (
            SpeechToTextProvider as _STT,
            TextToSpeechProvider as _TTS,
        )

        stt_registry = SpeechRegistry(
            storage=storage_provider.get_storage(_STT),
            factory=default_stt_factory,
            label="stt",
        )
        tts_registry = SpeechRegistry(
            storage=storage_provider.get_storage(_TTS),
            factory=default_tts_factory,
            label="tts",
        )
        app.state.stt_registry = stt_registry
        app.state.tts_registry = tts_registry
        logger.info("lifespan: speech registries constructed")

        logger.info("lifespan: web-fetch registry + service constructed")
        # Build the always-on `web` toolset (web-search dispatching via
        # the WebSearchService + http-request primitives). Reserved id
        # without underscore.
        logger.info("lifespan: building web toolset")
        web_toolset = build_web_toolset(
            web_search_service=web_search_service,
            web_fetch_service=web_fetch_service,
            workspace_registry=workspace_registry,
        )
        logger.info("lifespan: web toolset built")
        provider_registry._web_toolset_provider = web_toolset  # noqa: SLF001
        app.state.storage_provider = storage_provider
        app.state.secret_provider = secret_provider
        app.state.provider_registry = provider_registry
        app.state.workspace_registry = workspace_registry
        app.state.system_toolset = system_toolset
        app.state.misc_toolset = misc_toolset
        app.state.web_toolset = web_toolset
        app.state.internal_collections = None
        app.state.config = config

        # Resolve the cookie-signing secret (env var > db > auto-generate).
        # Stashed on app.state so the auth router + middleware can sign /
        # verify session cookies without re-reading from storage on every
        # request.
        if config.auth.enabled:
            from primer.auth.secret import resolve_session_secret

            session_secret = await resolve_session_secret(
                storage=storage_provider,
                auth_config=config.auth,
            )
            app.state.session_secret = session_secret
            logger.info("auth: session secret resolved")
        else:
            app.state.session_secret = None
            logger.warning("auth: disabled via config; running unauthenticated")

        # Graph router registry — singleton consumed by the worker
        # pool's _build_graph_executor when a graph has callable-router
        # edges (`_CallableRouter` discriminator). Empty at startup;
        # operators register callables via a startup hook or a future
        # POST /v1/_graph_routers/{id} endpoint. Graphs with only
        # static + json_path edges work fine without any registration.
        from primer.graph.router import RouterRegistry

        router_registry = RouterRegistry()
        app.state.router_registry = router_registry

        # --- Scheduler + worker pool wiring (Task 23) ----------------
        scheduler = None
        # Set when the wired scheduler/runtime-mode combination is unsafe
        # (surfaced on /v1/health as SchedulerHealth.degraded). None = healthy.
        app.state.scheduler_degraded_reason = None
        if scheduler_config is not None:
            from primer.scheduler.factory import SchedulerFactory

            logger.info("lifespan: creating scheduler")
            scheduler = SchedulerFactory.create(
                scheduler_config, storage_provider=storage_provider,
            )
            logger.info("lifespan: scheduler.initialize() begin")
            await scheduler.initialize()
            logger.info("lifespan: scheduler.initialize() done")
            # Loud warning: in-memory scheduler is single-process; running it
            # alongside any worker pool (whether colocated 'api+worker' or
            # separate 'worker' processes) means cross-process state is not
            # synchronised — a lease armed in one process is invisible to
            # another, and parked rows flipped to 'resumable' in shared storage
            # are never re-claimed. Sessions can be double-claimed or silently
            # stranded. Production should use the Postgres scheduler. See spec
            # §9.1. We both log loudly AND surface the condition on /v1/health
            # (SchedulerHealth.degraded) so a misconfigured deployment is
            # observable, not just buried in boot logs. There is no
            # strict/fail-fast config knob today, so this stays a degraded
            # signal rather than a hard ConfigError; add one here if a strict
            # mode is introduced.
            if (
                scheduler_config.provider == SchedulerProviderType.IN_MEMORY
                and config.runtime_mode != RuntimeMode.API
            ):
                degraded_reason = (
                    "in-memory scheduler with runtime_mode="
                    f"{config.runtime_mode.value} is not safe for multi-process "
                    "or external-worker deployment: each process has its own "
                    "claim engine, so leases and resumable parks are not shared "
                    "across processes. Switch to the Postgres scheduler for any "
                    "topology beyond a single process."
                )
                logger.warning("scheduler degraded: %s", degraded_reason)
                app.state.scheduler_degraded_reason = degraded_reason
        app.state.scheduler = scheduler

        # --- Event bus + yield background tasks (M2/M3) -------------
        # Bus drives the yielding-tool wake path: tool endpoints
        # publish; the listener mark_resumable()s parked rows; the
        # timer scheduler republishes due timer:* parks; the sweeper
        # catches expired non-timer parks. All bound to the same bus.
        event_bus = None
        claim_engine = None
        yield_listener = None
        timer_scheduler = None
        timeout_sweeper = None
        event_dispatcher = None
        event_retention_pruner = None
        workspace_event_bridge = None
        stuck_session_sweeper = None
        harness_sweeper = None
        watcher_manager = None
        mcp_task_bridge = None
        app.state.coordinator_sweeper = None
        app.state.event_dispatcher = None
        if scheduler is not None:
            from primer.bus.in_memory import InMemoryEventBus
            from primer.bus.listener import YieldEventListener
            from primer.bus.mcp_tasks import McpTaskBridge
            from primer.bus.postgres import PostgresEventBus
            from primer.bus.scheduler_tasks import (
                TimeoutSweeper, TimerScheduler,
            )
            from primer.bus.watcher import WatcherManager
            from primer.scheduler.postgres import PostgresScheduler

            # Pair the bus to the scheduler flavour: postgres scheduler
            # → LISTEN/NOTIFY bus (cross-app delivery); in-memory
            # scheduler → in-process bus.
            if isinstance(scheduler, PostgresScheduler):
                event_bus = PostgresEventBus(scheduler._storage)
            else:
                event_bus = InMemoryEventBus()
            logger.info("lifespan: event bus initialise")
            await event_bus.initialize()
            from primer.coordinator.factory import CoordinatorFactory as _CoordinatorFactory
            _owner_id = f"api-{_uuid.uuid4().hex[:12]}"
            coordinator = _CoordinatorFactory.create(
                storage_provider=storage_provider,
                event_bus=event_bus,
                owner_id=_owner_id,
            )
            app.state.coordinator = coordinator
            logger.info("lifespan: coordinator constructed")
            from primer.claim.factory import ClaimEngineFactory as _ClaimEngineFactory
            claim_engine = _ClaimEngineFactory.create(
                storage_provider=storage_provider,
                event_bus=event_bus,
            )
            logger.info("lifespan: claim engine constructed (%s)", type(claim_engine).__name__)
            await provider_registry.bind_invalidation_bus(coordinator.invalidation_bus)
            await provider_registry.bind_rate_limiter(coordinator.rate_limiter)
            # Re-bind the channel inbox now that the event bus exists.
            # ChannelInbox was constructed earlier (lines 102-104) with
            # event_bus=None because the bus is built later in the
            # lifespan (the bus depends on the scheduler, which depends
            # on the storage layer). Without this re-bind, the inbox's
            # publish path crashes with AttributeError on .publish.
            channel_inbox._event_bus = event_bus
            logger.info("lifespan: starting yield listener / timer / sweeper")
            from primer.model.workspace_session import (
                WorkspaceSession as _WorkspaceSession,
            )
            yield_listener = YieldEventListener(
                bus=event_bus,
                session_storage=storage_provider.get_storage(_WorkspaceSession),
                engine=claim_engine,
            )
            yield_listener.start()
            timer_scheduler = TimerScheduler(
                bus=event_bus,
                session_storage=storage_provider.get_storage(_WorkspaceSession),
                storage_provider=storage_provider,
            )
            timer_scheduler.start(coordinator.leader_elector)
            timeout_sweeper = TimeoutSweeper(
                bus=event_bus,
                session_storage=storage_provider.get_storage(_WorkspaceSession),
                storage_provider=storage_provider,
            )
            timeout_sweeper.start(coordinator.leader_elector)
            from primer.events.dispatcher import (
                EventDispatcher, EventRetentionPruner,
            )
            event_dispatcher = EventDispatcher(
                storage_provider=storage_provider,
                event_bus=event_bus,
                provider_registry=provider_registry,
                semantic_search_registry=semantic_search_registry,
                claim_engine=claim_engine,
                poll_seconds=config.event_dispatch_poll_seconds,
                batch=config.event_dispatch_batch,
                max_failures=config.event_sink_max_failures,
            )
            event_dispatcher.start(coordinator.leader_elector)
            event_retention_pruner = EventRetentionPruner(
                storage_provider=storage_provider,
                retention_days=config.event_retention_days,
            )
            event_retention_pruner.start(coordinator.leader_elector)
            app.state.event_dispatcher = event_dispatcher

            # Ends sessions whose first turn never ran. Without this a lost claim leaves a
            # non-terminal row forever, and a `parallelism="skip"` subscription will not fire
            # while one exists — so a single stuck session silently halts its trigger.
            from primer.bus.scheduler_tasks import StuckSessionSweeper
            stuck_session_sweeper = StuckSessionSweeper(
                session_storage=storage_provider.get_storage(_WorkspaceSession),
                # Without the engine the sweeper cannot tell a session whose first turn
                # is still running from one that never started, and defaults to reaping
                # neither. Passing it is what keeps the sweeper both safe and useful.
                claim_engine=claim_engine,
            )
            stuck_session_sweeper.start(coordinator.leader_elector)

            from primer.bus.scheduler_tasks import HarnessSweeper

            harness_sweeper = HarnessSweeper(
                storage_provider=storage_provider,
                scheduler=scheduler,
                event_bus=event_bus,
                provider_registry=provider_registry,
            )
            harness_sweeper.start(coordinator.leader_elector)

            # watch_files watcher manager — resolves workspace_id →
            # WatchProbe via the workspace registry.
            # Local workspaces expose a `root` Path → HostInotifyProbe.
            # Container / k8s workspaces expose `_sandbox` (a WSSandbox with
            # a RuntimeClient) + `_workspace_root` → WSWatchProbe.
            # Unknown / destroyed workspaces → None (manager skips).
            async def _resolve_probe(workspace_id: str):
                from primer.bus.host_inotify_probe import HostInotifyProbe
                from primer.bus.ws_watch_probe import WSWatchProbe
                from primer.workspace.runtime.ws_sandbox import WSSandbox
                try:
                    ws = await workspace_registry.get_workspace(workspace_id)
                except Exception:
                    return None
                # Local workspace exposes `root` (a Path).
                root = getattr(ws, "root", None)
                if root is not None:
                    return HostInotifyProbe(root=str(root))
                # Sandbox workspace (container / k8s) exposes a WSSandbox +
                # workspace_root.
                sandbox = getattr(ws, "_sandbox", None)
                workspace_root = getattr(ws, "_workspace_root", None)
                if isinstance(sandbox, WSSandbox) and workspace_root is not None:
                    return WSWatchProbe(
                        runtime_client=sandbox._client,
                        workspace_root=workspace_root,
                    )
                return None
            watcher_manager = WatcherManager(
                bus=event_bus,
                scheduler=scheduler,
                workspace_root_resolver=_resolve_probe,
                storage_provider=storage_provider,
            )
            watcher_manager.start(coordinator.leader_elector)
            logger.info("lifespan: watcher manager started")

            # Workspace lifecycle events (wave-2 spec Part 3): one
            # runtime stream per opted-in workspace, emitted onto the
            # platform event log by the leader.
            from primer.events.workspace_bridge import WorkspaceEventBridge

            async def _resolve_events_stream(workspace_id: str, config):
                from primer.workspace.runtime.ws_sandbox import WSSandbox
                try:
                    ws = await workspace_registry.get_workspace(workspace_id)
                except Exception:  # noqa: BLE001
                    return None
                sandbox = getattr(ws, "_sandbox", None)
                if isinstance(sandbox, WSSandbox):
                    return sandbox._client.events_subscribe(
                        kinds=list(config.kinds),
                        path_prefixes=config.path_prefixes,
                    )
                # Local workspace: host-side inotify serves the file
                # kind; there is no runtime to observe execs.
                root = getattr(ws, "root", None)
                if root is not None and "file_changed" in config.kinds:
                    from primer.bus.host_inotify_probe import HostInotifyProbe

                    async def _file_stream():
                        probe = HostInotifyProbe(root=str(root))
                        async for change in probe.watch(
                            list(config.path_prefixes or ["."]),
                        ):
                            yield {
                                "kind": "file_changed",
                                "path": change.path,
                                "mtime": change.mtime,
                                "size": change.size,
                            }

                    return _file_stream()
                return None

            workspace_event_bridge = WorkspaceEventBridge(
                storage_provider=storage_provider,
                stream_resolver=_resolve_events_stream,
                event_bus=event_bus,
            )
            workspace_event_bridge.start(coordinator.leader_elector)

            # MCP task bridge — polls parked mcp_task:* sessions
            # and republishes results onto the bus. The bridge looks
            # up the right MCP provider via the provider_registry, so
            # task-style tools across many MCP servers all funnel
            # through one bridge.
            mcp_task_bridge = McpTaskBridge(
                bus=event_bus,
                scheduler=scheduler,
                provider_registry=provider_registry,
                storage_provider=storage_provider,
            )
            mcp_task_bridge.start(coordinator.leader_elector)
            logger.info("lifespan: mcp task bridge started")

            # CoordinatorSweeper only runs against a Postgres-backed
            # storage provider — it issues SQL DELETEs through the
            # asyncpg pool. With InMemoryEventBus the storage is
            # SQLite (no .pool) so the sweep would crash every 30s.
            if isinstance(event_bus, PostgresEventBus):
                from primer.coordinator.sweeper import CoordinatorSweeper

                coordinator_sweeper = CoordinatorSweeper(storage_provider=storage_provider)
                coordinator_sweeper.start(coordinator.leader_elector)
                app.state.coordinator_sweeper = coordinator_sweeper
        app.state.event_bus = event_bus
        app.state.claim_engine = claim_engine
        # Action-event recorder: every non-CRUD emission site goes
        # through this. bus=None (scheduler disabled) still records;
        # the dispatcher then relies on its poll interval.
        from primer.events.recorder import EventRecorder as _EventRecorder
        app.state.event_recorder = _EventRecorder(
            storage_provider.get_event_store(), event_bus,
        )

        # Now that the claim engine exists, hand it to the channel registry so
        # warmed (and lazily built) adapters can wake the worker via
        # claim_engine.upsert on inbound chat messages. Then bring chat-driven
        # bots online: session channels start on the first outbound park, but a
        # chat is user-initiated and has no other start trigger, so warm the
        # enabled chat-channel adapters. Run it in the BACKGROUND so server
        # readiness is not gated on bot gateway connects (a Discord gateway can
        # take seconds to reach READY).
        channel_registry.set_claim_engine(claim_engine)
        channel_registry.set_session_wiring(
            workspace_registry=workspace_registry, scheduler=scheduler,
        )

        # Only a process that OWNS inbound may open channel gateways. Warming a
        # chat adapter opens its inbound listener (Telegram long-poll / Slack
        # socket / Discord gateway); doing that in a worker-only process would
        # be a SECOND inbound connection competing with the API's (Telegram 409
        # Conflict; duplicate Slack/Discord deliveries). Worker-only processes
        # do not open inbound at all, so they must NOT warm.
        owns_inbound = config.runtime_mode in (
            RuntimeMode.API, RuntimeMode.API_PLUS_WORKER,
        )

        if owns_inbound:
            app.state.chat_channel_warm_task = asyncio.create_task(
                warm_chat_channels(channel_registry),
            )
        else:
            app.state.chat_channel_warm_task = None

        # Process-local fan-out of session ticks to per-workspace tap
        # subscribers (the workspace dimension of the tick router). Owns
        # its own bus subscription — a second broadcast subscription
        # alongside the session-tick forwarder keeps the tap decoupled.
        # Built BEFORE the workspaces toolset so the ``workspace_tap``
        # drain tool can capture it for its bounded long-poll.
        workspace_tap_router = None
        if event_bus is not None:
            from primer.model.workspace_session import (
                WorkspaceSession as _WorkspaceSession,
            )
            from primer.tap.router import WorkspaceTapRouter

            workspace_tap_router = WorkspaceTapRouter(
                event_bus,
                storage_provider.get_storage(_WorkspaceSession),
            )
            await workspace_tap_router.start()
        app.state.workspace_tap_router = workspace_tap_router

        # Build the always-on _workspaces toolset now that the scheduler,
        # claim engine, and event bus exist (any may be None when no
        # scheduler is configured; the session tools degrade to an
        # ``unavailable`` error in that case).
        ws_toolset = build_workspaces_toolset(
            storage_provider=storage_provider,
            workspace_registry=workspace_registry,
            scheduler=scheduler,
            claim_engine=claim_engine,
            event_bus=event_bus,
            tap_router=workspace_tap_router,
        )
        provider_registry._workspaces_toolset_provider = ws_toolset  # noqa: SLF001
        app.state.workspaces_toolset = ws_toolset

        # --- Session recovery on startup -----------------------------------
        # The claim engine + scheduler are in-memory; their state does NOT
        # survive a process restart. Persisted WorkspaceSession rows DO.
        # Scan for non-ENDED rows and re-arm the engine so workers can
        # claim them again. Without this, a session created in the
        # previous process sits at status=RUNNING forever with no owner
        # — the diagnostic-report Bug 1.
        if claim_engine is not None:
            await recover_sessions(claim_engine, scheduler, storage_provider)

        # --- Chat recovery on startup --------------------------------------
        # Same shape as session recovery above but for the chat surface.
        # A chat row at turn_status='claimable' or 'running' with no
        # lease (because the worker died between writing a chat message
        # and releasing) would otherwise sit stuck forever — see
        # bug-2026-06-02T192011Z-8feeba2a. ChatClaimAdapter's
        # eligibility predicate requires turn_status in {claimable,
        # running} and chat.status='active', so we only re-arm rows
        # that match.
        # --- Webhook delivery recovery on startup --------------------------
        # The webhook endpoint persists a pending WebhookDelivery row before
        # its fire-and-forget BackgroundTask dispatches. A crash between the
        # 202 and dispatch completion leaves the row 'pending' and the
        # delivery lost (senders never retry a 202). Re-dispatch stale
        # pending rows via the same _dispatch_webhook path. Runs regardless
        # of claim_engine (fresh-session subs need it, but plain webhook
        # subs do not); the dispatcher tolerates a None claim_engine.
        # The sweep is one-shot, so rows still inside the grace window when it
        # runs would otherwise wait for the NEXT boot (possibly never). It
        # hands back a single task that re-checks exactly those ids once they
        # clear the window; we own it and cancel it on teardown so it cannot
        # outlive the app.
        _webhook_recheck_task = await recover_webhook_deliveries(
            storage_provider,
            event_bus,
            claim_engine,
            scheduler,
            workspace_registry,
        )
        app.state.webhook_recovery_recheck_task = _webhook_recheck_task

        # --- Observability: claim queue-depth sampler ----------------------
        # Runs every 10s when the claim engine is Postgres-backed and
        # metrics are enabled.  In-memory engine doesn't need it (the
        # metric would always be 0 outside tests).
        _claim_depth_task: asyncio.Task | None = None
        if (
            config.observability.enabled
            and config.observability.metrics_enabled
            and claim_engine is not None
        ):
            from primer.claim.postgres import PostgresClaimEngine as _PGClaimEngine
            if isinstance(claim_engine, _PGClaimEngine):
                _claim_depth_task = asyncio.ensure_future(
                    sample_claim_queue_depth(claim_engine)
                )
                logger.info("lifespan: claim queue-depth sampler started")

        # Build the always-on ``harness`` toolset. Needs event_bus so it
        # is constructed after the bus is wired (event_bus may be None
        # when running in API-only mode without a scheduler — the toolset
        # tolerates that: enqueue handlers skip publish when bus is None).
        harness_toolset = build_harness_toolset_provider(
            storage_provider=storage_provider,
            event_bus=event_bus,
        )
        provider_registry._harness_toolset_provider = harness_toolset  # noqa: SLF001
        app.state.harness_toolset = harness_toolset

        # Build the always-on ``trigger`` toolset (Phase 8). Like harness
        # it tolerates a None event_bus / claim_engine — the service
        # layer treats those collaborators as best-effort.
        trigger_toolset = build_trigger_toolset_provider(
            storage_provider=storage_provider,
            claim_engine=claim_engine,
            event_bus=event_bus,
        )
        provider_registry._trigger_toolset_provider = trigger_toolset  # noqa: SLF001
        app.state.trigger_toolset = trigger_toolset

        # Build the always-on ``workspace_ext`` toolset (workspace-only
        # yielding tools: sleep, watch_files, invoke_graph,
        # subscribe_to_trigger). Its tools are filtered out of chat tool
        # contexts at the ToolExecutionManager resolution choke point.
        workspace_ext_toolset = build_workspace_ext_toolset(
            storage_provider=storage_provider,
        )
        provider_registry._workspace_ext_toolset_provider = (  # noqa: SLF001
            workspace_ext_toolset
        )
        app.state.workspace_ext_toolset = workspace_ext_toolset

        # Build the always-on ``crud`` toolset (S5): agent/graph/trigger
        # construction plus python-toolset management, all behind the
        # seeded approval policies. Needs claim_engine/event_bus for the
        # trigger handlers, so it is built alongside the trigger toolset.
        crud_toolset = build_crud_toolset(
            storage_provider=storage_provider,
            claim_engine=claim_engine,
            event_bus=event_bus,
        )
        provider_registry._crud_toolset_provider = crud_toolset  # noqa: SLF001
        app.state.crud_toolset = crud_toolset

        from primer.bootstrap.seed import run_ensure_pass

        _ensure = await run_ensure_pass(
            storage_provider,
            workspace_registry=workspace_registry,
            toolset_providers={
                "system": system_toolset,
                "crud": crud_toolset,
                "workspaces": ws_toolset,
                "misc": misc_toolset,
                "web": web_toolset,
                "harness": harness_toolset,
                "trigger": trigger_toolset,
                "workspace_ext": workspace_ext_toolset,
                # The superseded standalone call also passed this one; the
                # /tools subtree loses the collections toolset without it.
                "collections": collections_toolset,
            },
            provider_registry=provider_registry,
            semantic_search_registry=semantic_search_registry,
            default_workspace_template=config.default_workspace_template,
        )
        if _ensure.errors:
            logger.warning("seed: ensure pass finished with errors", extra={
                "seed_errors": _ensure.errors,
            })
        # (The workspace tap router is constructed earlier, before the
        # workspaces toolset, so the ``workspace_tap`` drain tool can
        # capture it. See above.)

        worker_pool = None
        if config.runtime_mode in (
            RuntimeMode.WORKER, RuntimeMode.API_PLUS_WORKER,
        ):
            from primer.worker.pool import WorkerPool

            worker_pool = WorkerPool(
                config=config.worker,
                scheduler=scheduler,
                storage=storage_provider,
                workspace_registry=workspace_registry,
                provider_registry=provider_registry,
                semantic_search_registry=semantic_search_registry,
                router_registry=router_registry,
                approval_resolver=approval_resolver,
                channel_dispatcher=channel_dispatcher,
                event_bus=event_bus,
                artifact_storage_registry=artifact_storage_registry,
                engine=claim_engine,
            )
            logger.info("lifespan: worker_pool.start() begin")
            await worker_pool.start()
            logger.info("lifespan: worker_pool.start() done")
        app.state.worker_pool = worker_pool

        # Internal collections subsystem auto-activation: if a config
        # row already exists in storage, build the live subsystem +
        # search toolset and start the CDC worker. We do NOT auto-run
        # bootstrap here — the operator does that explicitly via
        # POST /v1/internal_collections/bootstrap.
        logger.info("lifespan: loading IC config")
        ic_config = await load_config_or_none(storage_provider)
        logger.info("lifespan: IC config loaded (present=%s)", ic_config is not None)

        # IC bootstrap recovery: if a bootstrap was in flight when the
        # previous API process exited, its asyncio task is gone but the
        # status row still says "running". Mark it as failed so the
        # UI surfaces the interruption and the operator can re-trigger.
        # Bootstrap recovery went with the boot-time vector pass.
        if ic_config is not None:
            ic_subsystem = build_subsystem(
                config=ic_config,
                storage_provider=storage_provider,
                provider_registry=provider_registry,
                semantic_search_registry=semantic_search_registry,
                toolset_providers={
                    # Every built-in (reserved-id) toolset must be listed
                    # here or its tools never get embedded and the
                    # _internal_tools semantic search misses them.
                    "system": system_toolset,
                    "workspaces": ws_toolset,
                    "misc": misc_toolset,
                    "web": web_toolset,
                    "harness": harness_toolset,
                    "trigger": trigger_toolset,
                    "workspace_ext": workspace_ext_toolset,
                    "collections": collections_toolset,
                },
            )
            app.state.internal_collections = ic_subsystem
            # The CDC vector worker no longer starts: vectorisation is
            # the explicit search lifecycle now. The subsystem stays
            # constructed so the reserved `search` toolset resolves.
        # Startup invariant: every kind the harness service manages must
        # appear in the CDC kinds registry.  _harness_kind_models() ensures
        # the registry is fully populated (handles test-reset and lazy-import
        # cases), then we assert no required kind is missing.
        # Note: EntityType (agent/graph/collection/tool) intentionally
        # omits "document" and "toolset" (no IC vector index for those),
        # so we check harness-managed storage kinds rather than EntityType.
        assert_harness_kinds_registered()

        # --- MCP server mount (/v1/mcp) ----------------------------------
        # Spec §4-5: StreamableHTTP MCP transport exposed at /v1/mcp,
        # gated by AuthMiddleware-populated scope state. The session
        # manager's run() is an async context that owns an anyio task
        # group; we enter it here and tear it down in the finally
        # block alongside the other long-lived services. Late mount
        # (after _mount_routers ran during create_app) is fine —
        # Starlette permits mid-lifespan mounts and the gate looks up
        # the session manager off app.state at request time.
        mcp_teardown = await _start_mcp_mount(
            app,
            storage_provider=storage_provider,
            provider_registry=provider_registry,
            approval_resolver=approval_resolver,
        )
        logger.info("lifespan: MCP /v1/mcp mounted")

        # Workspace health-probe loop. Pings each running/failed workspace
        # at ``workspace_probe_interval_seconds`` cadence, flips phase on
        # three-strike misses/hits. Started here — AFTER recover_sessions
        # and worker_pool.start() above, immediately before the app
        # declares itself ready — not up near workspace_registry's own
        # construction, so a booting pod structurally cannot probe before
        # it can actually serve: a freshly-started replacement pod used to
        # begin ticking (with a clean, in-process 0-miss streak) hundreds
        # of lines before the app had settled, so its first few pings
        # raced the app's own startup rather than reflecting real
        # workspace health, and three strikes (default 90s) was enough to
        # flip a perfectly healthy workspace to failed (01a0533c, live
        # SEV). WorkspaceProbeTask also waits one interval before its own
        # first tick (defense-in-depth for any other construction site).
        workspace_probe = WorkspaceProbeTask(
            storage_provider=storage_provider,
            registry=workspace_registry,
            interval_seconds=config.workspace_probe_interval_seconds,
        )
        app.state.workspace_probe = workspace_probe
        workspace_probe_runner = asyncio.create_task(
            workspace_probe.start(), name="workspace-probe",
        )
        app.state.workspace_probe_runner = workspace_probe_runner

        logger.info(
            "primer API ready",
            extra={"version": APP_VERSION, "host": config.host, "port": config.port},
        )
        try:
            yield
        finally:
            # Drain the MCP session manager early — its anyio task
            # group depends on the asyncio loop being alive.
            try:
                await mcp_teardown()
            except Exception:
                logger.exception("mcp session manager teardown failed")
            # Order matters: drain the pool first so in-flight turns get
            # a chance to settle while the scheduler is still alive,
            # then close the scheduler, then the rest of the
            # subsystems. Each step is guarded so a teardown failure
            # downstream still runs the others.
            # Stop the workspace probe early — independent of the
            # scheduler/bus, but it touches the workspace_registry
            # so it must finish before workspace_registry.aclose().
            try:
                workspace_probe.stop()
                try:
                    await asyncio.wait_for(
                        workspace_probe_runner, timeout=2.0,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    pass
            except Exception:
                logger.exception("workspace_probe stop failed")
            if worker_pool is not None:
                try:
                    await worker_pool.drain_and_stop()
                except Exception:
                    logger.exception("worker_pool.drain_and_stop failed")
            # Stop yield background tasks BEFORE the scheduler / bus
            # close so an in-flight tick doesn't race a closing bus.
            if getattr(app.state, "coordinator_sweeper", None) is not None:
                try:
                    await app.state.coordinator_sweeper.stop()
                except Exception:
                    logger.exception("coordinator_sweeper stop failed")
            for task, name in (
                (mcp_task_bridge, "mcp_task_bridge"),
                (watcher_manager, "watcher_manager"),
                (harness_sweeper, "harness_sweeper"),
                (workspace_event_bridge, "workspace_event_bridge"),
                (event_retention_pruner, "event_retention_pruner"),
                (event_dispatcher, "event_dispatcher"),
                (timeout_sweeper, "timeout_sweeper"),
                (stuck_session_sweeper, "stuck_session_sweeper"),
                (timer_scheduler, "timer_scheduler"),
                (yield_listener, "yield_listener"),
            ):
                if task is not None:
                    try:
                        await task.stop()
                    except Exception:
                        logger.exception("%s.stop failed", name)
            if workspace_tap_router is not None:
                try:
                    await workspace_tap_router.aclose()
                except Exception:
                    logger.exception("workspace_tap_router teardown failed")
            if _claim_depth_task is not None:
                try:
                    _claim_depth_task.cancel()
                    try:
                        await _claim_depth_task
                    except asyncio.CancelledError:
                        pass
                except Exception:
                    logger.exception("claim_depth_task teardown failed")
            # Sleeping until its grace-skipped ids clear the window. Anything
            # it had not re-fired yet stays pending for the next boot's sweep.
            if _webhook_recheck_task is not None:
                try:
                    _webhook_recheck_task.cancel()
                    try:
                        await _webhook_recheck_task
                    except asyncio.CancelledError:
                        pass
                except Exception:
                    logger.exception(
                        "webhook_recheck_task teardown failed"
                    )
            if event_bus is not None:
                try:
                    await event_bus.aclose()
                except Exception:
                    logger.exception("event_bus.aclose failed")
            if scheduler is not None:
                try:
                    await scheduler.aclose()
                except Exception:
                    logger.exception("scheduler.aclose failed")
            ic_subsystem = app.state.internal_collections
            if ic_subsystem is not None:
                try:
                    await ic_subsystem.aclose()
                except Exception:
                    logger.exception(
                        "internal_collections.aclose failed"
                    )
            try:
                await web_search_registry.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning("lifespan: web_search_registry aclose failed: %s", exc)
            try:
                await web_fetch_registry.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning("lifespan: web_fetch_registry aclose failed: %s", exc)
            try:
                await stt_registry.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning("lifespan: stt_registry aclose failed: %s", exc)
            try:
                await tts_registry.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning("lifespan: tts_registry aclose failed: %s", exc)
            try:
                await channel_registry.aclose()
            except Exception:
                logger.exception("channel_registry.aclose failed")
            try:
                await provider_registry.aclose()
            except Exception:
                logger.exception("provider_registry.aclose failed")
            try:
                await semantic_search_registry.aclose()
            except Exception:
                logger.exception("semantic_search_registry.aclose failed")
            try:
                asr = getattr(app.state, "artifact_storage_registry", None)
                if asr is not None:
                    await asr.aclose()
            except Exception:
                logger.exception("artifact_storage_registry.aclose failed")
            try:
                await workspace_registry.aclose()
            except Exception:
                logger.exception("workspace_registry.aclose failed")
            try:
                await secret_provider.aclose()
            except Exception:
                logger.warning("lifespan: secret_provider.aclose() failed", exc_info=True)
            try:
                await storage_provider.aclose()
            except Exception:
                logger.exception("storage_provider.aclose failed")

    return _lifespan
