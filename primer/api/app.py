"""FastAPI app factory + lifespan handler."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware


class _GZipExceptMcp(GZipMiddleware):
    """Bypass gzip for paths under ``/v1/mcp``.

    The global :class:`GZipMiddleware` buffers + compresses response
    bodies, which breaks the chunked SSE stream the MCP
    StreamableHTTP transport relies on (no flush boundaries; the
    client never sees an event until the body completes). Other
    endpoints continue to benefit from compression unchanged.
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/v1/mcp"):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)

from primer.api.config import AppConfig
from primer.api.errors import register_error_handlers
from primer.api._jsx_bundle import build_jsx_bundle
from primer.api.registries import (
    ProviderRegistry,
    SemanticSearchRegistry,
    WorkspaceRegistry,
)
from primer.model.provider import SemanticSearchProvider
from primer.api.routers import (
    chats as chats_router,
    compute,
    health,
    internal_collections,
    knowledge,
    providers,
    sessions as sessions_router,
    workers as workers_router,
    workspaces as workspaces_router,
    yields as yields_router,
)
from primer.api.routers.auth import auth_router
from primer.api.routers.semantic_search import semantic_search_router
from primer.api.routers.user_docs import user_docs_router
from primer.api.routers.web_fetch import (
    web_fetch_active_config_router,
    web_fetch_providers_helpers_router,
    web_fetch_providers_router,
)
from primer.api.routers.web_search import (
    web_search_active_config_router,
    web_search_providers_helpers_router,
    web_search_providers_router,
)
from primer.api.version import API_VERSION, APP_VERSION
from primer.internal_collections import build_subsystem, load_config_or_none

# Importing these modules registers channel adapter factories with
# primer.channel.factory. Safe at module level; defers the
# heavyweight platform-SDK imports until the first Channel of
# that provider type is constructed.
import primer.channel.slack.factory  # noqa: F401
import primer.channel.telegram.factory  # noqa: F401
import primer.channel.discord.factory  # noqa: F401
from primer.model.scheduler import RuntimeMode, SchedulerProviderType
from primer.toolset.harness import build_harness_toolset_provider
from primer.toolset.misc import build_misc_toolset
from primer.toolset.search import build_search_toolset
from primer.toolset.system import build_system_toolset
from primer.toolset.trigger import build_trigger_toolset_provider
from primer.toolset.web import build_web_toolset
from primer.toolset.workspaces import build_workspaces_toolset
from primer.workspace.probe import WorkspaceProbeTask


if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)


async def _bootstrap_web_search(storage_provider) -> None:
    """Idempotent: ensure the reserved DDG provider row + active
    config singleton exist. Called from the lifespan handler before
    the web toolset is built.

    Order matters: the DDG row must exist before the active config
    singleton, because the singleton's reference validation runs at
    write time."""
    from primer.model.web_search import (
        ACTIVE_WEB_SEARCH_CONFIG_ID,
        ActiveWebSearchConfig,
        DuckDuckGoConfig,
        SingleProviderConfig,
        WebSearchProvider,
        WebSearchProviderType,
    )

    from primer.model.except_ import ConflictError

    ws_storage = storage_provider.get_storage(WebSearchProvider)
    if await ws_storage.get("DuckDuckGo") is None:
        try:
            await ws_storage.create(WebSearchProvider(
                id="DuckDuckGo",
                provider_type=WebSearchProviderType.DUCKDUCKGO,
                config=DuckDuckGoConfig(),
            ))
            logger.info(
                "bootstrap: created reserved web-search provider DuckDuckGo"
            )
        except ConflictError:
            # Cross-process bootstrap race: another primer process created
            # the reserved row between our get() and create(). The desired
            # end state (the row exists) holds, so this is a no-op.
            logger.debug("bootstrap: DuckDuckGo row created concurrently")

    ac_storage = storage_provider.get_storage(ActiveWebSearchConfig)
    if await ac_storage.get(ACTIVE_WEB_SEARCH_CONFIG_ID) is None:
        try:
            await ac_storage.create(ActiveWebSearchConfig(
                id=ACTIVE_WEB_SEARCH_CONFIG_ID,
                config=SingleProviderConfig(provider_id="DuckDuckGo"),
            ))
            logger.info(
                "bootstrap: created reserved active web-search config "
                "(single -> DuckDuckGo)"
            )
        except ConflictError:
            logger.debug(
                "bootstrap: active web-search config created concurrently"
            )


async def _bootstrap_web_fetch(storage_provider) -> None:
    """Idempotent: ensure the reserved LOCAL provider row + active config
    singleton (single -> local) exist. Mirrors _bootstrap_web_search."""
    from primer.model.web_fetch import (
        ACTIVE_WEB_FETCH_CONFIG_ID, ActiveWebFetchConfig, LocalFetchConfig,
        SingleFetchConfig, WebFetchProvider, WebFetchProviderType,
    )
    from primer.model.except_ import ConflictError

    wf_storage = storage_provider.get_storage(WebFetchProvider)
    if await wf_storage.get("local") is None:
        try:
            await wf_storage.create(WebFetchProvider(
                id="local", provider_type=WebFetchProviderType.LOCAL,
                config=LocalFetchConfig(),
            ))
            logger.info("bootstrap: created reserved web-fetch provider local")
        except ConflictError:
            logger.debug("bootstrap: web-fetch local row created concurrently")

    ac_storage = storage_provider.get_storage(ActiveWebFetchConfig)
    if await ac_storage.get(ACTIVE_WEB_FETCH_CONFIG_ID) is None:
        try:
            await ac_storage.create(ActiveWebFetchConfig(
                id=ACTIVE_WEB_FETCH_CONFIG_ID,
                config=SingleFetchConfig(provider_id="local"),
            ))
            logger.info("bootstrap: created reserved active web-fetch config (single -> local)")
        except ConflictError:
            logger.debug("bootstrap: active web-fetch config created concurrently")


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

        storage_provider = _build_storage_provider(config)
        await storage_provider.initialize()

        # --- First-boot auto-bootstrap -----------------------------------
        # Run synchronously before serving so the reserved-id providers
        # are available by the time any request arrives. Cost <2s on
        # warm disk (models download lazily, not here).
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

        semantic_search_registry = SemanticSearchRegistry(
            storage=storage_provider.get_storage(SemanticSearchProvider),
        )
        app.state.semantic_search_registry = semantic_search_registry

        # Artifact storage (chat media bytes). Build the registry and seed the
        # reserved default DB-backed provider so media works with zero operator
        # config. Idempotent: a concurrent boot may race the create.
        from primer.api.registries.artifact_storage_registry import (
            DEFAULT_ARTIFACT_PROVIDER_ID,
            ArtifactStorageRegistry,
        )
        from primer.model.provider import ArtifactStorageProvider
        _asp_storage = storage_provider.get_storage(ArtifactStorageProvider)
        artifact_storage_registry = ArtifactStorageRegistry(
            storage=_asp_storage,
            storage_provider=storage_provider,
        )
        app.state.artifact_storage_registry = artifact_storage_registry
        try:
            if await _asp_storage.get(DEFAULT_ARTIFACT_PROVIDER_ID) is None:
                await _asp_storage.create(ArtifactStorageProvider(
                    id=DEFAULT_ARTIFACT_PROVIDER_ID, provider="db",
                ))
                logger.info(
                    "bootstrap: created reserved default artifact provider (db)",
                )
        except Exception:
            logger.exception("seeding default artifact provider failed")

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
        )
        provider_registry._system_toolset_provider = system_toolset  # noqa: SLF001
        # NOTE: the always-on _workspaces toolset is built later in this
        # lifespan (after the scheduler/claim_engine/event_bus exist) so its
        # session tools can capture those deps. See the build below.
        # Build the always-on _misc toolset (stateless utilities).
        misc_toolset = build_misc_toolset()
        provider_registry._misc_toolset_provider = misc_toolset  # noqa: SLF001
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
        logger.info("lifespan: web-fetch registry + service constructed")
        # Build the always-on `web` toolset (web-search dispatching via
        # the WebSearchService + http-request primitives). Reserved id
        # without underscore.
        logger.info("lifespan: building web toolset")
        web_toolset = build_web_toolset(
            web_search_service=web_search_service,
            web_fetch_service=web_fetch_service,
        )
        logger.info("lifespan: web toolset built")
        provider_registry._web_toolset_provider = web_toolset  # noqa: SLF001
        app.state.storage_provider = storage_provider
        app.state.provider_registry = provider_registry
        app.state.workspace_registry = workspace_registry
        app.state.system_toolset = system_toolset
        app.state.misc_toolset = misc_toolset
        app.state.web_toolset = web_toolset
        app.state.internal_collections = None
        app.state.search_toolset = None
        app.state.config = config

        # Construct the user-docs service. Walks primer/user_docs/ once
        # at startup; the service handles its own mtime-based hot-reload
        # from then on. Stash on app.state so the router can reach it.
        import primer
        from primer.user_docs_service import UserDocsService

        _user_docs_root = Path(primer.__file__).resolve().parent / "user_docs"
        user_docs_service = UserDocsService(_user_docs_root)
        user_docs_service.reload_index()
        app.state.user_docs_service = user_docs_service
        _registry_path = _user_docs_root / "_fixtures" / "registry.json"
        try:
            _registry_data = json.loads(_registry_path.read_text(encoding="utf-8"))
            _user_docs_embed_ids: list[str] = _registry_data.get("embeds", [])
        except Exception:  # noqa: BLE001
            logger.warning(
                "lifespan: could not read embed registry from %s; "
                "no embed ids will be valid",
                _registry_path,
            )
            _user_docs_embed_ids = []
        app.state.user_docs_embeds = _user_docs_embed_ids
        user_docs_service.set_embeds_manifest(_user_docs_embed_ids)
        logger.info("lifespan: user-docs service initialised")
        # Dev-mode lint gate. Set PRIMER_USER_DOCS_STRICT=1 to refuse
        # startup on lint errors. Production logs them and excludes
        # the offending docs from the manifest.
        _ud_errors = [
            i for i in user_docs_service.lint_issues()
            if i.severity == "error"
        ]
        _ud_warnings = [
            i for i in user_docs_service.lint_issues()
            if i.severity == "warning"
        ]
        if _ud_warnings:
            logger.warning(
                "user_docs: %d lint warning(s); see /docs/_lint",
                len(_ud_warnings),
            )
        if _ud_errors:
            _ud_summary = "\n".join(
                f"  {i.file}:{i.line or '?'} [{i.rule}] {i.message}"
                for i in _ud_errors[:20]
            )
            if os.environ.get("PRIMER_USER_DOCS_STRICT") == "1":
                raise RuntimeError(
                    f"user_docs: {len(_ud_errors)} lint error(s); "
                    f"refusing to start.\n{_ud_summary}"
                )
            logger.error(
                "user_docs: %d lint error(s).\n%s",
                len(_ud_errors), _ud_summary,
            )

        # Workspace health-probe loop. Pings each running/failed
        # workspace at ``workspace_probe_interval_seconds`` cadence,
        # flips phase on three-strike misses/hits. Lives next to the
        # workspace_registry (its sole non-storage dependency).
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
            # synchronised — sessions can be double-claimed. Production should
            # use the Postgres scheduler. See spec §9.1.
            if (
                scheduler_config.provider == SchedulerProviderType.IN_MEMORY
                and config.runtime_mode != RuntimeMode.API
            ):
                logger.warning(
                    "in-memory scheduler with runtime_mode=%s is not safe for "
                    "multi-worker deployment; switch to Postgres for production",
                    config.runtime_mode.value,
                )
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
        chat_sweeper = None
        harness_sweeper = None
        watcher_manager = None
        mcp_task_bridge = None
        app.state.coordinator_sweeper = None
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
            )
            timer_scheduler.start(coordinator.leader_elector)
            timeout_sweeper = TimeoutSweeper(
                bus=event_bus,
                session_storage=storage_provider.get_storage(_WorkspaceSession),
            )
            timeout_sweeper.start(coordinator.leader_elector)

            from primer.bus.scheduler_tasks import ChatSweeper, HarnessSweeper
            chat_sweeper = ChatSweeper(
                storage_provider=storage_provider,
                scheduler=scheduler,
                event_bus=event_bus,
            )
            chat_sweeper.start(coordinator.leader_elector)

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
            )
            watcher_manager.start(coordinator.leader_elector)
            logger.info("lifespan: watcher manager started")

            # MCP task bridge — polls parked mcp_task:* sessions
            # and republishes results onto the bus. The bridge looks
            # up the right MCP provider via the provider_registry, so
            # task-style tools across many MCP servers all funnel
            # through one bridge.
            mcp_task_bridge = McpTaskBridge(
                bus=event_bus,
                scheduler=scheduler,
                provider_registry=provider_registry,
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

        # Now that the claim engine exists, hand it to the channel registry so
        # warmed (and lazily built) adapters can wake the worker via
        # claim_engine.upsert on inbound chat messages. Then bring chat-driven
        # bots online: session channels start on the first outbound park, but a
        # chat is user-initiated and has no other start trigger, so warm the
        # enabled chat-channel adapters. Run it in the BACKGROUND so server
        # readiness is not gated on bot gateway connects (a Discord gateway can
        # take seconds to reach READY).
        channel_registry.set_claim_engine(claim_engine)

        # Only a process that OWNS inbound may open channel gateways. Warming a
        # chat adapter opens its inbound listener (Telegram long-poll / Slack
        # socket / Discord gateway); doing that in a worker-only process would
        # be a SECOND inbound connection competing with the API's (Telegram 409
        # Conflict; duplicate Slack/Discord deliveries). Worker-only processes
        # relay outbound over the bus instead (see _forward_chat_relays_from_bus
        # below + ChatChannelDispatcher), so they must NOT warm.
        owns_inbound = config.runtime_mode in (
            RuntimeMode.API, RuntimeMode.API_PLUS_WORKER,
        )

        async def _warm_chat_channels() -> None:
            try:
                warmed = await channel_registry.warm_chat_channels()
                if warmed:
                    logger.info("warmed %d chat-channel adapter(s)", warmed)
            except Exception:
                logger.exception("warm_chat_channels failed during startup")

        if owns_inbound:
            app.state.chat_channel_warm_task = asyncio.create_task(
                _warm_chat_channels(),
            )
        else:
            app.state.chat_channel_warm_task = None

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
            try:
                from primer.int.claim import ClaimKind as _ClaimKind
                from primer.model.storage import OffsetPage as _OffsetPage
                from primer.model.workspace_session import (
                    SessionStatus as _SessionStatus,
                    WorkspaceSession as _WorkspaceSession,
                )

                _session_storage = storage_provider.get_storage(_WorkspaceSession)
                _recovered_running = 0
                _recovered_other = 0
                _offset = 0
                while True:
                    _page = await _session_storage.list(
                        _OffsetPage(offset=_offset, length=200)
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

        # --- Chat recovery on startup --------------------------------------
        # Same shape as session recovery above but for the chat surface.
        # A chat row at turn_status='claimable' or 'running' with no
        # lease (because the worker died between writing a chat message
        # and releasing) would otherwise sit stuck forever — see
        # bug-2026-06-02T192011Z-8feeba2a. ChatClaimAdapter's
        # eligibility predicate requires turn_status in {claimable,
        # running} and chat.status='active', so we only re-arm rows
        # that match.
        if claim_engine is not None:
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
                async def _sample_claim_queue_depth() -> None:
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

                _claim_depth_task = asyncio.ensure_future(_sample_claim_queue_depth())
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

        # Process-local router for chat tick events. One bus subscription
        # per process feeds it; WS handlers subscribe per-chat.
        from primer.chat.tick_router import ChatTickRouter, Tick

        chat_tick_router = ChatTickRouter()
        app.state.chat_tick_router = chat_tick_router

        async def _forward_chat_ticks_from_bus() -> None:
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

        if event_bus is not None:
            chat_tick_task = asyncio.create_task(
                _forward_chat_ticks_from_bus(),
                name="chat-tick-forwarder",
            )
            app.state.chat_tick_forwarder_task = chat_tick_task
        else:
            chat_tick_task = None
            app.state.chat_tick_forwarder_task = None

        # Chat -> channel relay forwarder. An out-of-proc worker cannot post to
        # a channel (it deliberately does not own the inbound gateway), so it
        # publishes a tiny ``chat:<id>:relay`` signal; the inbound-owning
        # process re-derives the text/gate from storage and posts via its warm
        # adapter. Only runs where inbound lives (API / api+worker). In a
        # single api+worker process the worker posts directly via the shared
        # warm registry and never publishes, so this stays idle there.
        async def _forward_chat_relays_from_bus() -> None:
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

        if event_bus is not None and owns_inbound:
            chat_relay_task = asyncio.create_task(
                _forward_chat_relays_from_bus(),
                name="chat-relay-forwarder",
            )
            app.state.chat_relay_forwarder_task = chat_relay_task
        else:
            chat_relay_task = None
            app.state.chat_relay_forwarder_task = None

        # Process-local router for session tick events. One bus subscription
        # per process feeds it; WS handlers subscribe per-session.
        from primer.session.tick_router import SessionTickRouter
        from primer.session.tick_router import Tick as SessionTick

        session_tick_router = SessionTickRouter()
        app.state.session_tick_router = session_tick_router

        async def _forward_session_ticks_from_bus() -> None:
            sub = event_bus.subscribe()
            try:
                async for event in sub:
                    key = event.event_key
                    if not key.startswith("session:") or not key.endswith(":tick"):
                        continue
                    sid = key[len("session:"):-len(":tick")]
                    if not sid:
                        continue
                    seq = event.payload.get("seq") if event.payload else None
                    if not isinstance(seq, int):
                        continue
                    session_tick_router._publish(sid, SessionTick(seq=seq))
            except asyncio.CancelledError:
                pass
            finally:
                await sub.aclose()

        if event_bus is not None:
            session_tick_task = asyncio.create_task(
                _forward_session_ticks_from_bus(),
                name="session-tick-forwarder",
            )
            app.state.session_tick_forwarder_task = session_tick_task
        else:
            session_tick_task = None
            app.state.session_tick_forwarder_task = None

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
                chat_tick_router=chat_tick_router,
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
                },
            )
            search_toolset = build_search_toolset(ic_subsystem)
            ic_subsystem.register_toolset_provider("search", search_toolset)
            provider_registry._search_toolset_provider = search_toolset  # noqa: SLF001
            app.state.internal_collections = ic_subsystem
            app.state.search_toolset = search_toolset
            ic_subsystem.start_worker()
        # Startup invariant: every kind the harness service manages must
        # appear in the CDC kinds registry.  _harness_kind_models() ensures
        # the registry is fully populated (handles test-reset and lazy-import
        # cases), then we assert no required kind is missing.
        # Note: EntityType (agent/graph/collection/tool) intentionally
        # omits "document" and "toolset" (no IC vector index for those),
        # so we check harness-managed storage kinds rather than EntityType.
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

        # --- Document vector backfill ------------------------------------
        # Index any user document whose vector chunks are missing (stored
        # before the embed-on-ingest hook existed, or whose embedding failed
        # at ingest time). Cheap and idempotent: on a healthy boot where
        # everything is already indexed, no embeds run. Best-effort so a bad
        # embedder never blocks startup.
        try:
            from primer.knowledge.indexing import (  # noqa: PLC0415
                backfill_missing_document_vectors,
            )
            await backfill_missing_document_vectors(
                storage_provider=storage_provider,
                provider_registry=provider_registry,
                semantic_search_registry=semantic_search_registry,
            )
        except Exception:
            logger.exception("lifespan: document vector backfill failed")

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
                except (asyncio.TimeoutError, asyncio.CancelledError):
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
                (chat_sweeper, "chat_sweeper"),
                (timeout_sweeper, "timeout_sweeper"),
                (timer_scheduler, "timer_scheduler"),
                (yield_listener, "yield_listener"),
            ):
                if task is not None:
                    try:
                        await task.stop()
                    except Exception:
                        logger.exception("%s.stop failed", name)
            if chat_tick_task is not None:
                try:
                    chat_tick_task.cancel()
                    try:
                        await chat_tick_task
                    except asyncio.CancelledError:
                        pass
                except Exception:
                    logger.exception("chat_tick_task teardown failed")
            if session_tick_task is not None:
                try:
                    session_tick_task.cancel()
                    try:
                        await session_tick_task
                    except asyncio.CancelledError:
                        pass
                except Exception:
                    logger.exception("session_tick_task teardown failed")
            if chat_relay_task is not None:
                try:
                    chat_relay_task.cancel()
                    try:
                        await chat_relay_task
                    except asyncio.CancelledError:
                        pass
                except Exception:
                    logger.exception("chat_relay_task teardown failed")
            if _claim_depth_task is not None:
                try:
                    _claim_depth_task.cancel()
                    try:
                        await _claim_depth_task
                    except asyncio.CancelledError:
                        pass
                except Exception:
                    logger.exception("claim_depth_task teardown failed")
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
                await storage_provider.aclose()
            except Exception:
                logger.exception("storage_provider.aclose failed")

    return _lifespan


def _build_storage_provider(config: AppConfig) -> "StorageProvider":
    """Construct the storage provider from the AppConfig.

    When ``config.db`` is None, default to embedded SQLite at
    ``~/.primer/db/data.sqlite``. The parent directory is created
    on demand inside :meth:`SqliteStorageProvider.initialize`.
    """
    from primer.model.provider import (
        SqliteConfig as _SqliteConfig,
        StorageProviderConfig as _StorageProviderConfig,
        StorageProviderType as _StorageProviderType,
    )
    from primer.storage.factory import StorageProviderFactory

    from primer.model.provider import PostgresConfig as _PostgresConfig

    sp_config = config.db
    if sp_config is None:
        default_path = Path.home() / ".primer" / "db" / "data.sqlite"
        sp_config = _StorageProviderConfig(
            provider=_StorageProviderType.SQLITE,
            config=_SqliteConfig(path=default_path),
        )

    # PRIMER_DB_SCHEMA overrides the Postgres schema for test isolation.
    # SQLite has no schema concept, so the override is silently ignored
    # when the backend is SQLite.
    if config.db_schema is not None and isinstance(sp_config.config, _PostgresConfig):
        sp_config = sp_config.model_copy(
            update={"config": sp_config.config.model_copy(
                update={"db_schema": config.db_schema}
            )}
        )

    return StorageProviderFactory.create(sp_config)


def _mount_routers(
    app: FastAPI,
    runtime_mode: RuntimeMode = RuntimeMode.API_PLUS_WORKER,
) -> None:
    """Mount routers under the API version prefix.

    In :class:`RuntimeMode.WORKER` mode only the always-on observability
    surface (``/v1/health`` and ``/v1/workers``) is mounted — entity
    routers (workspaces, sessions, providers, knowledge, internal
    collections, compute) are skipped because the worker process does
    not serve external traffic.
    """
    from fastapi import Depends
    from primer.api.deps import require_auth

    prefix = f"/{API_VERSION}"
    # Always-on routers — health probes + worker observability/drain.
    # Public: no auth dep so liveness/readiness probes work pre-login.
    app.include_router(health.router, prefix=prefix)
    app.include_router(workers_router.router, prefix=prefix)
    # Auth router — mounted unconditionally so login/register work in
    # all runtime modes. The router itself enforces single-user-v1.
    # Public by design (the endpoints ARE the auth surface).
    app.include_router(auth_router, prefix=prefix)
    if runtime_mode == RuntimeMode.WORKER:
        return

    # Everything below requires a valid session cookie. require_auth
    # is applied at include-router time so router internals don't need
    # to change (Commit 6).
    auth_dep = [Depends(require_auth)]

    # API token CRUD — sibling of the auth router under /v1/auth/tokens.
    # Cookie-only by router-internal check; bearer-authenticated callers
    # are rejected so a bearer token can't mint or manage other tokens.
    from primer.api.routers.api_tokens import api_tokens_router
    app.include_router(api_tokens_router, prefix=prefix, dependencies=auth_dep)

    # Phase 1 — providers + tools
    app.include_router(providers.llm_provider_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(providers.embedding_provider_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(providers.cross_encoder_provider_router, prefix=prefix, dependencies=auth_dep)
    # builtin_toolsets_router MUST be registered before toolset_router so
    # GET /toolsets/builtin is matched by the literal route rather than
    # being captured as toolset_id="builtin" by the CRUD GET-by-id.
    app.include_router(providers.builtin_toolsets_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(providers.toolset_router, prefix=prefix, dependencies=auth_dep)
    # Spec B §3.4 — flat tool catalogue for the graph editor's ToolCall
    # picker. Sibling of providers.builtin_toolsets_router's nested
    # ``GET /tools`` (which the operator console's tool/agent pages use);
    # mounted at the disambiguated ``/tools/catalogue`` path.
    from primer.api.routers.tools import tools_router
    app.include_router(tools_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(semantic_search_router, prefix=prefix, dependencies=auth_dep)
    from primer.api.routers.artifact_storage import artifact_storage_router
    app.include_router(artifact_storage_router, prefix=prefix, dependencies=auth_dep)
    # web_search_providers_helpers_router MUST be registered before
    # web_search_providers_router so GET /web_search_providers/_types is
    # matched by the literal route rather than being captured as id="_types"
    # by the CRUD GET-by-id. Same for POST /_test.
    app.include_router(web_search_providers_helpers_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(web_search_providers_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(web_search_active_config_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(web_fetch_providers_helpers_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(web_fetch_providers_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(web_fetch_active_config_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(user_docs_router, prefix=prefix, dependencies=auth_dep)
    # Phase 2 — compute (Agent + Graph)
    app.include_router(compute.agent_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(compute.graph_router, prefix=prefix, dependencies=auth_dep)
    # Phase 3 — knowledge (Collection + Document).
    app.include_router(knowledge.collection_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(knowledge.document_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(internal_collections.router, prefix=prefix, dependencies=auth_dep)
    # Workspaces (providers, templates, workspaces + sub-resources).
    app.include_router(workspaces_router.provider_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(workspaces_router.template_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(workspaces_router.workspace_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(workspaces_router.sessions_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(workspaces_router.files_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(workspaces_router.log_router, prefix=prefix, dependencies=auth_dep)
    # Sessions.
    app.include_router(sessions_router.nested_session_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(sessions_router.top_session_router, prefix=prefix, dependencies=auth_dep)
    # Yields.
    app.include_router(yields_router.yields_router, prefix=prefix, dependencies=auth_dep)
    # Chat REST. NOTE: the WS endpoint inside this router cannot use
    # the same dep mechanism (FastAPI does not enforce auth deps on
    # WebSocket routes the same way). The WS handler reads
    # request.state.user manually after upgrade — see chats.py.
    app.include_router(chats_router.chats_router, prefix=prefix, dependencies=auth_dep)
    # Tool approval policies.
    from primer.api.routers.tool_approval import make_tool_approval_router
    app.include_router(make_tool_approval_router(), prefix=prefix, dependencies=auth_dep)
    # Channel providers and channels.
    from primer.api.routers.channels import (
        make_channel_provider_router,
        make_channel_router,
    )
    app.include_router(make_channel_provider_router(), prefix=prefix, dependencies=auth_dep)
    app.include_router(make_channel_router(), prefix=prefix, dependencies=auth_dep)
    # Harness REST router.
    from primer.api.routers.harness import harness_router
    app.include_router(harness_router, dependencies=auth_dep)
    # Triggers REST router (Spec §10).
    from primer.api.routers.triggers import triggers_router
    app.include_router(triggers_router, dependencies=auth_dep)
    # MCP exposure CRUD — Spec §10. Cookie-gated for writes (the
    # router itself rejects bearer-token PUTs); reads pass through.
    from primer.api.routers.mcp_exposure import mcp_exposure_router
    app.include_router(mcp_exposure_router, prefix=prefix, dependencies=auth_dep)
    # Bug reporter — write-only POST that saves reports to disk.
    # Cookie-gated like everything else under /v1 so casual scrapers
    # can't drop files in the configured bugs/ directory.
    from primer.api.routers.bugs import bugs_router
    app.include_router(bugs_router, prefix=prefix, dependencies=auth_dep)
    # Instrumentation endpoints — only mounted when the env var is set.
    # Public to keep the distributed test harness simple; the env var
    # itself is the access gate.
    import os as _os
    if _os.environ.get("PRIMER_ENABLE_TEST_ENDPOINTS") == "1":
        from primer.api.routers._test_endpoints import router as _test_router
        app.include_router(_test_router, prefix=prefix)


def create_app(config: AppConfig) -> FastAPI:
    """Production factory: builds the app + wires the lifespan handler."""
    # Disable Swagger / ReDoc UIs unless the operator opts back in via
    # the log_level=debug setting; the OpenAPI JSON stays under the
    # /v1/ prefix to match the rest of the versioned API surface.
    # Swagger + ReDoc are always mounted: the API itself is exposed
    # regardless of log_level, so hiding the doc surface is security
    # theater and breaks the console's "View OpenAPI" affordance.
    app = FastAPI(
        title="Primer Microagents Framework API",
        version=APP_VERSION,
        lifespan=_make_lifespan(config),
        contact={"name": "primer"},
        openapi_url=f"/{API_VERSION}/openapi.json",
        docs_url=f"/{API_VERSION}/docs",
        redoc_url=f"/{API_VERSION}/redoc",
    )
    # Gzip body for >=1KB responses. Negligible CPU for static UI
    # assets (~700 KB total → ~120 KB on the wire), bypasses tiny
    # JSON envelopes, and skips WebSockets entirely (different ASGI
    # scope). Binary downloads (application/octet-stream) pass through
    # with a small CPU hit but no corruption.
    app.add_middleware(_GZipExceptMcp, minimum_size=1024)
    _install_security_headers(app)
    _install_console_csp(app)
    _install_request_id(app)
    _install_auth_middleware(app)
    _mount_routers(app, runtime_mode=config.runtime_mode)
    # JSX bundle route MUST be registered before the /console static
    # mount so it wins the route match for /console/_app.js.
    _install_jsx_bundle(app)
    _mount_console(app)
    _mount_metrics(app, config)
    _install_root_redirect(app)
    register_error_handlers(app)
    return app


def _mount_metrics(app: FastAPI, config: AppConfig) -> None:
    """Mount the Prometheus ``/metrics`` endpoint when metrics are enabled.

    The endpoint is mounted via :func:`prometheus_client.make_asgi_app`
    which returns a bare ASGI application wrapping the Primer-specific
    :data:`primer.observability.metrics.registry`.  Mounting happens
    *before* error handlers so the mount does not go through FastAPI's
    exception machinery.

    When ``config.observability.metrics_enabled`` is *False* or
    ``config.observability.enabled`` is *False* the mount is skipped
    entirely and ``GET /metrics`` returns a 404.
    """
    if not config.observability.enabled or not config.observability.metrics_enabled:
        return

    from prometheus_client import make_asgi_app as _make_metrics_asgi
    from primer.observability.metrics import registry as _metrics_registry

    metrics_app = _make_metrics_asgi(registry=_metrics_registry)
    app.mount("/metrics", metrics_app)


def _install_root_redirect(app: FastAPI) -> None:
    """GET / -> 307 redirect to /console/.

    Operators land at the host root expecting the console; without this
    they get a bare 404 from FastAPI. The console mount handles its own
    trailing-slash redirect from /console -> /console/.
    """
    from starlette.responses import RedirectResponse

    @app.get("/", include_in_schema=False)
    async def _root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/console/", status_code=307)


def _install_auth_middleware(app: FastAPI) -> None:
    """Install the cookie-auth middleware.

    Populates ``request.state.user`` / ``.principal`` from a signed
    ``primer_session`` cookie. Does not itself 401; routers do that
    via :func:`primer.api.deps.require_auth`.
    """
    from primer.api.middleware.auth import AuthMiddleware

    app.add_middleware(AuthMiddleware)


async def _mcp_send_simple_response(send, status, body, extra_headers=None):
    """Emit a minimal JSON response from the MCP auth gate.

    The gate runs before the SDK's session manager touches the scope,
    so we cannot lean on FastAPI's exception machinery to render
    errors. A hand-rolled ASGI start+body pair keeps the surface
    tight and avoids accidentally inheriting any of the SDK's own
    response shaping.
    """
    import json
    body_bytes = json.dumps(body).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body_bytes)).encode("ascii")),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": headers,
    })
    await send({"type": "http.response.body", "body": body_bytes})


def _make_mcp_auth_gate(app: FastAPI):
    """Build the ASGI gate that fronts ``StreamableHTTPSessionManager``.

    Reads scope state populated by :class:`AuthMiddleware`
    (``state.user`` / ``state.principal`` / ``state.api_token``),
    rejects anonymous callers with 401 + ``WWW-Authenticate``, and
    enforces the ``mcp`` scope on bearer tokens with 403. Cookie
    sessions carry full user authority (``api_token is None``) and
    pass through without a scope check.

    On success the principal + api_token id are stashed in the
    module-level :class:`ContextVar`s :data:`current_principal` and
    :data:`current_api_token_id` (from :mod:`primer.mcp.server`) so
    the MCP request handlers see the authenticated caller. The
    ContextVars are reset in a ``finally`` so concurrent requests on
    the same worker do not leak identities.
    """
    from primer.mcp.server import (
        current_api_token_id as _current_api_token_id,
        current_principal as _current_principal,
    )
    from starlette.datastructures import State

    async def _mcp_auth_gate(scope, receive, send):
        if scope["type"] != "http":
            # WebSocket / lifespan scopes are not part of the MCP
            # surface; reject quietly so a stray probe doesn't crash.
            await _mcp_send_simple_response(
                send, 400, {"detail": {"code": "unsupported_scope"}},
            )
            return

        state = scope.get("state")
        # AuthMiddleware sets ``state`` to a Starlette ``State`` object;
        # support both that and a plain dict for defensive callers.
        if isinstance(state, State):
            user = getattr(state, "user", None)
            principal = getattr(state, "principal", None)
            api_token = getattr(state, "api_token", None)
        elif isinstance(state, dict):
            user = state.get("user")
            principal = state.get("principal")
            api_token = state.get("api_token")
        else:
            user = principal = api_token = None

        if user is None:
            await _mcp_send_simple_response(
                send, 401,
                {"detail": {"code": "auth_required"}},
                extra_headers=[
                    (b"www-authenticate", b'Bearer realm="primer"'),
                ],
            )
            return

        if api_token is not None and "mcp" not in api_token.scopes:
            await _mcp_send_simple_response(
                send, 403,
                {"detail": {"code": "scope_required", "scope": "mcp"}},
            )
            return

        session_manager = getattr(app.state, "mcp_session_manager", None)
        if session_manager is None:
            # Should never happen in a well-configured app — surface a
            # 503 rather than crash, so the failure is visible to ops.
            await _mcp_send_simple_response(
                send, 503,
                {"detail": {"code": "mcp_unavailable"}},
            )
            return

        principal_tok = _current_principal.set(principal)
        api_token_id_tok = _current_api_token_id.set(
            api_token.id if api_token is not None else None
        )
        try:
            await session_manager.handle_request(scope, receive, send)
        finally:
            _current_principal.reset(principal_tok)
            _current_api_token_id.reset(api_token_id_tok)

    return _mcp_auth_gate


async def _start_mcp_mount(
    app: FastAPI,
    *,
    storage_provider,
    provider_registry,
    approval_resolver=None,
):
    """Build the MCP session manager, mount /v1/mcp, return a teardown.

    The session manager's ``run()`` is an async context manager that
    spins an anyio task group; entered here, exited by the returned
    coroutine. Callers (the production lifespan + the test factory)
    are responsible for invoking the teardown during shutdown so the
    task group can drain.
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from primer.mcp.exposure import ExposureDeps
    from primer.mcp.server import build_mcp_server

    def _deps_factory():
        return ExposureDeps(
            storage_provider=storage_provider,
            provider_registry=provider_registry,
            approval_resolver=approval_resolver,
        )

    mcp_server = build_mcp_server(_deps_factory)
    mcp_session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        json_response=False,
        stateless=False,
    )
    _ctx = mcp_session_manager.run()
    await _ctx.__aenter__()
    app.state.mcp_session_manager = mcp_session_manager
    # Mount once. The gate closure captures ``app`` so it can read
    # the session manager off ``app.state`` at request time; this
    # also keeps the mount survivable across hot-reloads in tests
    # that rebuild the manager without re-mounting.
    app.mount("/v1/mcp", _make_mcp_auth_gate(app))

    async def _teardown() -> None:
        try:
            await _ctx.__aexit__(None, None, None)
        finally:
            app.state.mcp_session_manager = None

    return _teardown


def _install_security_headers(app: FastAPI) -> None:
    """Set conservative defensive headers on every response.

    The API is JSON-only, so a strict ``no-sniff`` + deny-frame policy
    is safe by default. ``Cross-Origin-Resource-Policy: same-origin``
    blocks no-CORS embeds from other origins. CSP for the JSON surface
    is handled by ``_install_console_csp`` which scopes the policy to
    the ``/console/*`` mount only — JSON responses never carry one.
    """
    @app.middleware("http")
    async def _security_headers(request, call_next):  # noqa: ARG001
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin",
        )
        response.headers.setdefault(
            "Cross-Origin-Resource-Policy", "same-origin",
        )
        return response


# CSP header for the /console/* static mount.
#
# Why 'unsafe-eval' AND 'unsafe-inline' are both required:
# @babel/standalone has two paths for running <script type="text/babel">
# tags. (1) ``new Function`` over the transpiled body — covered by
# 'unsafe-eval'. (2) When the source comes from a `src=` attribute it
# fetches the file, transpiles it, and injects a fresh <script> element
# whose body is the transpiled code INLINE — that's an inline script
# and CSP blocks it without 'unsafe-inline'. The .jsx files are all
# `src`-loaded, so path (2) is what we hit; the console page renders
# blank without 'unsafe-inline'.
#
# Why there are NO `sha384-...` entries in script-src:
# CSP hash source-list entries (`'sha-*'`) allow inline script BLOCKS
# whose content hashes to a listed value — they are NOT a way to pin
# external script integrity. External-script integrity is enforced by
# the `integrity="sha384-..."` attribute on the `<script src=...>` tag
# (Subresource Integrity, a separate browser layer). More importantly,
# per CSP spec the presence of ANY hash/nonce in script-src causes
# 'unsafe-inline' to be silently ignored — defeating the inline-script
# allowance we need for Babel path (2). The CDN script integrity is
# preserved unchanged by the `integrity=` attributes already on the
# script tags in `ui/index.html`.
#
# Trust chain after this CSP:
#   1. CDN scripts (React, ReactDOM, Babel-standalone) load only from
#      `https://unpkg.com` and are verified by SRI on the script tag.
#   2. .jsx files load only from `'self'`.
#   3. `connect-src 'self'` blocks all exfiltration to other origins.
#   4. The XSS path 'unsafe-inline' normally opens — injected inline
#      <script> in served HTML — has no entry point here: nothing
#      user-controlled lands in /console/* content. An attacker would
#      need write access to ui/ directly, at which point CSP is moot.
#   5. The alternative — pre-compile the JSX at build time — requires
#      an npm-installed Babel CLI, which the project forbids on the
#      host (Shai-Hulud mitigation).
# Documented in docs/superpowers/specs/2026-05-15-web-console-implementation-design.md §2.2.
_CONSOLE_CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-eval' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)


def _install_console_csp(app: FastAPI) -> None:
    """Apply a strict CSP only to ``/console/*`` responses.

    JSON responses on ``/v1/*`` are not browser-renderable so CSP has no
    effect on them. Scoping the policy to the static UI mount keeps the
    JSON surface unchanged and avoids any unintended interaction with
    OpenAPI / Swagger / ReDoc when log_level=debug is set.
    """
    @app.middleware("http")
    async def _console_csp(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/console"):
            # Direct assignment, not setdefault — the policy is strict
            # by intent; no downstream handler should be loosening it.
            response.headers["Content-Security-Policy"] = _CONSOLE_CSP
            # Cache-Control is set per-file inside _CachingStaticFiles
            # (immutable for ui/vendor/*, no-cache for index.html,
            # short-lived public for everything else). Don't blanket
            # it here or the StaticFiles values get clobbered.
        return response


# Directory containing the operator console (the bind-mounted ui/
# folder at repo root). Computed once at import time; the production
# factory guards on .is_dir() so a deployment that strips the directory
# still boots without the console mount.
_UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui"


# Request-id propagation. Honour an incoming X-Request-Id when it
# parses as a safe token; otherwise mint a fresh one. Stashed on
# request.state.request_id so error handlers can embed it into the
# RFC 7807 envelope (extensions.request_id) and the UI's "Copy
# request id" action has something to surface.
#
# Defensive guard on the incoming value: cap length + restrict to a
# conservative character set so a malicious client cannot smuggle
# control characters / log-injection payloads through the header
# (the value is echoed on the response and logged structurally).
import re as _re
import uuid as _uuid

_VALID_REQUEST_ID = _re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


def _install_request_id(app: FastAPI) -> None:
    """Stamp X-Request-Id on every response; expose it via request.state.

    Incoming X-Request-Id values are honoured when they match the
    conservative regex above; otherwise a fresh ``req-<uuid hex[:12]>``
    is generated. The id is set on the response header and stashed at
    ``request.state.request_id`` for downstream consumers (the error
    mapper threads it into ``extensions.request_id``).
    """
    @app.middleware("http")
    async def _request_id(request, call_next):
        incoming = request.headers.get("X-Request-Id")
        if incoming and _VALID_REQUEST_ID.match(incoming):
            rid = incoming
        else:
            rid = "req-" + _uuid.uuid4().hex[:12]
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response


def _install_jsx_bundle(app: FastAPI) -> None:
    """Precompile every text/babel script at startup, register a route
    that serves the concatenated bundle at ``/console/_app.js``.

    Why a Route instead of writing the bundle to disk: keeps the
    repo's ``ui/`` tree clean (no build artefacts), and the in-memory
    body is what every subsequent request reads anyway.

    Cache strategy: short max-age + strong ETag, so reloads after a
    backend redeploy revalidate quickly (304 when nothing changed,
    fresh bytes when bundle hash flipped) without needing the URL
    to embed the hash.
    """
    from starlette.responses import Response

    etag, body = build_jsx_bundle(_UI_DIR)
    if not body:
        # No UI dir or no Babel — leave route unregistered; the
        # console will 404 on /_app.js and the static mount handles
        # the rest as before.
        return

    @app.get("/console/_app.js", include_in_schema=False)
    async def _serve_jsx_bundle(request: Request) -> Response:
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={
                "ETag": etag,
                "Cache-Control": "public, max-age=300, must-revalidate",
            })
        return Response(
            content=body,
            media_type="application/javascript",
            headers={
                "ETag": etag,
                "Cache-Control": "public, max-age=300, must-revalidate",
            },
        )


class _CachingStaticFiles(StaticFiles):
    """StaticFiles + path-aware Cache-Control.

    Caching strategy:

    * ``index.html``  → ``no-cache`` so any deploy is picked up on
      next navigation. Sub-resources it references are still subject
      to their own per-file policy below.
    * ``vendor/*``    → ``public, max-age=1y, immutable``. These are
      pinned third-party builds (see ui/vendor/MANIFEST.md); when we
      bump a version the filename will change anyway.
    * everything else → ``public, max-age=300, must-revalidate``.
      Short enough that an edited .jsx/.css shows up in the browser
      within five minutes without a hard refresh, long enough that
      asset-heavy panels don't hit the network on every navigation.

    Starlette's StaticFiles already emits Last-Modified, so the
    must-revalidate path is a cheap 304 round-trip rather than a
    full re-download.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        if response.status_code != 200:
            return response
        # Starlette normalises bare ``/console/`` to ``"."`` via
        # os.path.normpath(""), and html=True maps that to
        # ``index.html`` internally — so cover both spellings.
        if path in ("", ".", "index.html"):
            response.headers["Cache-Control"] = "no-cache"
        elif path.startswith("vendor/") or path.startswith("vendor" + os.sep):
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
            )
        else:
            response.headers["Cache-Control"] = (
                "public, max-age=300, must-revalidate"
            )
        return response


def _mount_console(app: FastAPI) -> None:
    """Mount the operator console at ``/console`` if the ui/ dir is present.

    ``html=True`` makes StaticFiles serve ``index.html`` for the bare
    ``/console/`` prefix. Only invoked from :func:`create_app` (the
    production factory); tests intentionally do not get the static
    mount.
    """
    if _UI_DIR.is_dir():
        app.mount(
            "/console",
            _CachingStaticFiles(directory=str(_UI_DIR), html=True),
            name="console",
        )
    else:
        logger.info(
            "ui/ directory not found at %s; /console mount skipped",
            _UI_DIR,
        )


def create_test_app(
    *,
    storage_provider: "StorageProvider",
    provider_registry: ProviderRegistry,
    workspace_registry: WorkspaceRegistry | None = None,
    system_toolset=None,
    workspaces_toolset=None,
    misc_toolset=None,
    web_toolset=None,
    start_chat_worker: bool = False,
) -> FastAPI:
    """Test factory: skips the lifespan; stashes pre-built dependencies.

    If any of ``system_toolset``, ``workspace_registry``,
    ``workspaces_toolset``, or ``misc_toolset`` is omitted the factory
    builds one against the supplied registries — the same wiring the
    production lifespan performs. Pass an explicit instance to inject
    a stub.
    """
    app = FastAPI(
        title="Primer Microagents Framework API (test)",
        version=APP_VERSION,
        contact={"name": "primer"},
    )
    _install_request_id(app)
    _install_auth_middleware(app)
    if workspace_registry is None:
        workspace_registry = WorkspaceRegistry(storage_provider)
    # Wire the SemanticSearchRegistry so /v1/ssp endpoints work in tests.
    from primer.model.provider import SemanticSearchProvider
    _test_ssp_registry = SemanticSearchRegistry(
        storage=storage_provider.get_storage(SemanticSearchProvider),
        factory=lambda row: object(),  # type: ignore[arg-type]
    )
    from primer.agent.approval import ApprovalResolver as _AR
    from primer.model.tool_approval import ToolApprovalPolicy as _TAP
    _test_approval_resolver = _AR(
        storage=storage_provider.get_storage(_TAP),
    )
    app.state.approval_resolver = _test_approval_resolver
    if system_toolset is None:
        system_toolset = build_system_toolset(
            storage_provider=storage_provider,
            provider_registry=provider_registry,
            semantic_search_registry=_test_ssp_registry,
        )
    if workspaces_toolset is None:
        # The test factory builds its scheduler/event_bus/claim_engine
        # later (and only conditionally), so the session tools degrade to
        # an ``unavailable`` error here. Tests that exercise them inject a
        # pre-built ``workspaces_toolset`` instead.
        workspaces_toolset = build_workspaces_toolset(
            storage_provider=storage_provider,
            workspace_registry=workspace_registry,
            scheduler=None,
            claim_engine=None,
            event_bus=None,
        )
    if misc_toolset is None:
        misc_toolset = build_misc_toolset()
    if web_toolset is None:
        # Build a real WebSearchService over the test storage so the
        # web-search tool can dispatch in tests. Note: tests that
        # exercise the tool end-to-end must seed the active-config row
        # and at least one provider; tests that don't will see
        # WebSearchProviderError surfaced as a tool error envelope.
        from primer.api.registries.web_search_registry import (
            WebSearchRegistry as _WSR,
            default_web_search_factory as _default_factory,
        )
        from primer.model.web_search import (
            ActiveWebSearchConfig as _ActiveCfg,
            WebSearchProvider as _WSP,
        )
        from primer.web_search.service import WebSearchService as _WSS
        _test_ws_registry = _WSR(
            storage=storage_provider.get_storage(_WSP),
            factory=_default_factory,
        )
        _test_ws_service = _WSS(
            registry=_test_ws_registry,
            active_config_storage=storage_provider.get_storage(_ActiveCfg),
        )
        app.state.web_search_registry = _test_ws_registry
        app.state.web_search_service = _test_ws_service
        from primer.api.registries.web_fetch_registry import (
            WebFetchRegistry as _WFR,
            default_web_fetch_factory as _default_wf_factory,
        )
        from primer.model.web_fetch import (
            ActiveWebFetchConfig as _ActiveWFCfg,
            WebFetchProvider as _WFP,
        )
        from primer.web_fetch.service import WebFetchService as _WFS
        _test_wf_registry = _WFR(
            storage=storage_provider.get_storage(_WFP),
            factory=_default_wf_factory,
        )
        _test_wf_service = _WFS(
            registry=_test_wf_registry,
            active_config_storage=storage_provider.get_storage(_ActiveWFCfg),
        )
        app.state.web_fetch_registry = _test_wf_registry
        app.state.web_fetch_service = _test_wf_service
        web_toolset = build_web_toolset(
            web_search_service=_test_ws_service,
            web_fetch_service=_test_wf_service,
        )
    provider_registry._system_toolset_provider = system_toolset  # noqa: SLF001
    provider_registry._workspaces_toolset_provider = workspaces_toolset  # noqa: SLF001
    provider_registry._misc_toolset_provider = misc_toolset  # noqa: SLF001
    provider_registry._web_toolset_provider = web_toolset  # noqa: SLF001
    app.state.storage_provider = storage_provider
    app.state.provider_registry = provider_registry
    app.state.workspace_registry = workspace_registry
    app.state.system_toolset = system_toolset
    app.state.workspaces_toolset = workspaces_toolset
    app.state.misc_toolset = misc_toolset
    app.state.web_toolset = web_toolset
    app.state.semantic_search_registry = _test_ssp_registry
    # Artifact storage registry + reserved default (parity with the lifespan).
    from primer.api.registries.artifact_storage_registry import (
        DEFAULT_ARTIFACT_PROVIDER_ID as _ART_DEFAULT,
        ArtifactStorageRegistry as _ArtReg,
    )
    from primer.model.provider import ArtifactStorageProvider as _ASP
    _art_storage = storage_provider.get_storage(_ASP)
    app.state.artifact_storage_registry = _ArtReg(
        storage=_art_storage, storage_provider=storage_provider,
    )

    async def _seed_artifact_default() -> None:
        try:
            if await _art_storage.get(_ART_DEFAULT) is None:
                await _art_storage.create(_ASP(id=_ART_DEFAULT, provider="db"))
        except Exception:
            logger.exception("test app: seeding default artifact provider failed")

    app.state.seed_artifact_default = _seed_artifact_default
    # Tests build the subsystem on demand via the /bootstrap endpoint.
    app.state.internal_collections = None
    app.state.search_toolset = None
    # Wire the user-docs service over the real primer/user_docs/ tree;
    # tests that exercise /v1/user_docs see the live manifest + docs on
    # disk. Hot-reload via mtime still works in tests.
    import primer as _primer_pkg
    from primer.user_docs_service import UserDocsService as _UDS
    _user_docs_root = (
        Path(_primer_pkg.__file__).resolve().parent / "user_docs"
    )
    _test_user_docs_service = _UDS(_user_docs_root)
    _test_user_docs_service.reload_index()
    app.state.user_docs_service = _test_user_docs_service
    _test_embed_ids = [
        "topbar",
        "sessions-list-empty",
        "agent-create-modal",
        "graph-canvas-three-nodes",
        "channels-prompt",
        "docs-callout-demo",
        "workspace-empty",
        "session-detail-panel",
        "chat-stream",
        "harness-wizard-step",
        "workspace-template-form",
        "collection-list-empty",
        "ssp-list",
        "trigger-create",
        "worker-stats",
        "api-token-create",
        "bug-reporter-modal",
    ]
    app.state.user_docs_embeds = _test_embed_ids
    _test_user_docs_service.set_embeds_manifest(_test_embed_ids)
    # Auth: tests get a fixed test secret so cookies are deterministic
    # across the suite. Real lifespan uses resolve_session_secret().
    from primer.api.config import AppConfig
    app.state.config = AppConfig()
    app.state.session_secret = "test-session-secret-32-bytes-aaaaaaa"
    # Attach an in-memory scheduler so the /workers router has something
    # to depend on.
    from primer.scheduler.in_memory import InMemoryScheduler
    _test_scheduler = InMemoryScheduler(storage_provider=storage_provider)
    app.state.scheduler = _test_scheduler
    app.state.worker_pool = None
    # Attach an in-memory event bus so yielding-tool endpoints (ask_user,
    # tool_approval respond) can publish without raising ConfigError.
    # Tests that need to inspect published events may monkey-patch
    # app.state.event_bus.publish before sending their request.
    from primer.bus.in_memory import InMemoryEventBus
    _test_event_bus = InMemoryEventBus()
    app.state.event_bus = _test_event_bus

    from primer.chat.tick_router import ChatTickRouter as _CTR, Tick as _Tick

    _chat_tick_router = _CTR()
    app.state.chat_tick_router = _chat_tick_router

    async def _start_chat_tick_forwarder() -> asyncio.Task:
        """Async helper for the test fixture — create_test_app
        intentionally skips the lifespan, so the test must call this
        from within an active event loop to spin the forwarder task."""
        sub = app.state.event_bus.subscribe()

        async def _loop() -> None:
            try:
                async for event in sub:
                    key = event.event_key
                    if not key.startswith("chat:") or not key.endswith(":tick"):
                        continue
                    cid = key[len("chat:"):-len(":tick")]
                    if not cid:
                        continue
                    seq = event.payload.get("seq") if event.payload else None
                    if isinstance(seq, int):
                        _chat_tick_router.publish(cid, _Tick(seq=seq))
            except asyncio.CancelledError:
                pass
            finally:
                await sub.aclose()

        return asyncio.create_task(_loop(), name="chat-tick-forwarder")

    app.state.start_chat_tick_forwarder = _start_chat_tick_forwarder

    from primer.session.tick_router import SessionTickRouter as _STR
    from primer.session.tick_router import Tick as _SessionTick

    _session_tick_router = _STR()
    app.state.session_tick_router = _session_tick_router

    async def _start_session_tick_forwarder() -> asyncio.Task:
        """Async helper for the test fixture — spin the session tick
        forwarder task within an active event loop."""
        sub = app.state.event_bus.subscribe()

        async def _session_loop() -> None:
            try:
                async for event in sub:
                    key = event.event_key
                    if not key.startswith("session:") or not key.endswith(":tick"):
                        continue
                    sid = key[len("session:"):-len(":tick")]
                    if not sid:
                        continue
                    seq = event.payload.get("seq") if event.payload else None
                    if isinstance(seq, int):
                        _session_tick_router._publish(sid, _SessionTick(seq=seq))
            except asyncio.CancelledError:
                pass
            finally:
                await sub.aclose()

        return asyncio.create_task(_session_loop(), name="session-tick-forwarder")

    app.state.start_session_tick_forwarder = _start_session_tick_forwarder

    # Optional worker pool for integration tests that need the chat
    # claim loop (start_chat_worker=True).
    if start_chat_worker:
        from primer.model.scheduler import WorkerConfig as _WorkerConfig
        from primer.worker.pool import WorkerPool as _WorkerPool

        _pool_config = _WorkerConfig(
            concurrency=4,
            claim_batch_size=2,
            heartbeat_interval_seconds=5,
            lease_ttl_seconds=15,
            poll_interval_seconds=0.1,
            drain_timeout_seconds=5,
        )
        from primer.claim.factory import ClaimEngineFactory as _CEF
        _claim_engine = _CEF.create(
            storage_provider=storage_provider,
            event_bus=_test_event_bus,
        )
        _pool = _WorkerPool(
            config=_pool_config,
            scheduler=_test_scheduler,
            storage=storage_provider,
            workspace_registry=workspace_registry,
            provider_registry=provider_registry,
            semantic_search_registry=_test_ssp_registry,
            event_bus=_test_event_bus,
            chat_tick_router=_chat_tick_router,
            artifact_storage_registry=app.state.artifact_storage_registry,
            engine=_claim_engine,
        )
        app.state.worker_pool = _pool
        app.state.claim_engine = _claim_engine

        async def _start_worker_pool() -> None:
            await _pool.start()

        async def _stop_worker_pool() -> None:
            await _pool.drain_and_stop(timeout=2.0)

        app.state.start_worker_pool = _start_worker_pool
        app.state.stop_worker_pool = _stop_worker_pool
    else:
        app.state.start_worker_pool = None
        app.state.stop_worker_pool = None

    # Channel subsystem — registry + dispatcher for test fixtures.
    from primer.api.registries.channel_registry import ChannelRegistry as _CR
    from primer.channel.dispatcher import ChannelDispatcher as _CD
    from primer.channel.inbox import ChannelInbox as _CI
    from primer.model.channel import (
        Channel as _Channel,
        ChannelProvider as _ChannelProvider,
    )
    _test_channel_inbox = _CI(event_bus=None)
    _test_channel_registry = _CR(
        channel_storage=storage_provider.get_storage(_Channel),
        channel_provider_storage=storage_provider.get_storage(_ChannelProvider),
        inbox=_test_channel_inbox,
        storage_provider=storage_provider,
    )
    _test_channel_dispatcher = _CD(registry=_test_channel_registry)
    app.state.channel_inbox = _test_channel_inbox
    app.state.channel_registry = _test_channel_registry
    app.state.channel_dispatcher = _test_channel_dispatcher
    _mount_routers(app)
    register_error_handlers(app)

    # MCP mount helpers. ASGITransport doesn't drive the lifespan, so
    # we expose explicit start/stop coroutines and let the test
    # fixture call them around the yield. Mirrors the
    # start_chat_tick_forwarder pattern above.
    app.state.mcp_session_manager = None
    _mcp_teardown_holder: dict[str, object] = {"fn": None}

    async def _start_mcp() -> None:
        if _mcp_teardown_holder["fn"] is not None:
            return
        _mcp_teardown_holder["fn"] = await _start_mcp_mount(
            app,
            storage_provider=storage_provider,
            provider_registry=provider_registry,
            approval_resolver=_test_approval_resolver,
        )

    async def _stop_mcp() -> None:
        fn = _mcp_teardown_holder["fn"]
        if fn is None:
            return
        _mcp_teardown_holder["fn"] = None
        await fn()  # type: ignore[misc]

    app.state.start_mcp_mount = _start_mcp
    app.state.stop_mcp_mount = _stop_mcp

    # When start_chat_worker=True, attach a lifespan so SyncTestClient
    # (which drives the ASGI lifespan) starts the forwarder + worker pool
    # in the same event loop as the app. This ensures the WS tick
    # subscription and the worker pool share the same asyncio event loop,
    # preventing cross-loop asyncio.Queue issues.
    if start_chat_worker:
        @asynccontextmanager
        async def _test_lifespan(_a: FastAPI) -> AsyncIterator[None]:
            fwd_task = await _a.state.start_chat_tick_forwarder()
            sess_fwd_task = await _a.state.start_session_tick_forwarder()
            await _a.state.start_worker_pool()
            try:
                yield
            finally:
                try:
                    await _a.state.stop_worker_pool()
                except Exception:
                    pass
                fwd_task.cancel()
                try:
                    await fwd_task
                except asyncio.CancelledError:
                    pass
                sess_fwd_task.cancel()
                try:
                    await sess_fwd_task
                except asyncio.CancelledError:
                    pass

        app.router.lifespan_context = _test_lifespan  # type: ignore[assignment]

    return app


__all__ = ["create_app", "create_test_app"]
