"""FastAPI app factory + lifespan handler."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from matrix.api.config import AppConfig
from matrix.api.errors import register_error_handlers
from matrix.api.registries import (
    ProviderRegistry,
    SemanticSearchRegistry,
    WorkspaceRegistry,
)
from matrix.model.provider import SemanticSearchProvider
from matrix.api.routers import (
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
from matrix.api.routers.semantic_search import semantic_search_router
from matrix.api.version import API_VERSION, APP_VERSION
from matrix.internal_collections import build_subsystem, load_config_or_none
from matrix.model.scheduler import RuntimeMode, SchedulerProviderType
from matrix.toolset.misc import build_misc_toolset
from matrix.toolset.search import build_search_toolset
from matrix.toolset.system import build_system_toolset
from matrix.toolset.web import build_web_toolset
from matrix.toolset.workspaces import build_workspaces_toolset


if TYPE_CHECKING:
    from matrix.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)


def _make_lifespan(config: AppConfig):
    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        # When scheduler is unset, default to an in-memory scheduler
        # so the zero-config path boots. Operators running worker mode
        # in production should explicitly set a Postgres scheduler.
        scheduler_config = config.scheduler
        if scheduler_config is None and config.runtime_mode in (
            RuntimeMode.WORKER, RuntimeMode.API_PLUS_WORKER,
        ):
            from matrix.model.scheduler import (
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
        semantic_search_registry = SemanticSearchRegistry(
            storage=storage_provider.get_storage(SemanticSearchProvider),
        )
        app.state.semantic_search_registry = semantic_search_registry

        from matrix.agent.approval import ApprovalResolver
        from matrix.model.tool_approval import ToolApprovalPolicy

        approval_resolver = ApprovalResolver(
            storage=storage_provider.get_storage(ToolApprovalPolicy),
        )
        app.state.approval_resolver = approval_resolver
        workspace_registry = WorkspaceRegistry(storage_provider)
        # Bootstrap the system toolset before constructing the
        # ProviderRegistry so the registry can short-circuit
        # ``get_toolset('_system')`` to it.
        # Resolve the MCP stdio allowlist from AppConfig and bake it into
        # the toolset factory so every MCP provider built from a row is
        # consistently constrained.
        from matrix.api.registries.provider_registry import (
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
        )
        system_toolset = build_system_toolset(
            storage_provider=storage_provider,
            provider_registry=provider_registry,
            semantic_search_registry=semantic_search_registry,
        )
        provider_registry._system_toolset_provider = system_toolset  # noqa: SLF001
        # Build the always-on _workspaces toolset.
        ws_toolset = build_workspaces_toolset(
            storage_provider=storage_provider,
            workspace_registry=workspace_registry,
        )
        provider_registry._workspaces_toolset_provider = ws_toolset  # noqa: SLF001
        # Build the always-on _misc toolset (stateless utilities).
        misc_toolset = build_misc_toolset()
        provider_registry._misc_toolset_provider = misc_toolset  # noqa: SLF001
        # Build the always-on `web` toolset (DuckDuckGo search +
        # http-request primitives). Reserved id without underscore.
        logger.info("lifespan: building web toolset")
        web_toolset = build_web_toolset()
        logger.info("lifespan: web toolset built")
        provider_registry._web_toolset_provider = web_toolset  # noqa: SLF001
        app.state.storage_provider = storage_provider
        app.state.provider_registry = provider_registry
        app.state.workspace_registry = workspace_registry
        app.state.system_toolset = system_toolset
        app.state.workspaces_toolset = ws_toolset
        app.state.misc_toolset = misc_toolset
        app.state.web_toolset = web_toolset
        app.state.internal_collections = None
        app.state.search_toolset = None

        # Graph router registry — singleton consumed by the worker
        # pool's _build_graph_executor when a graph has callable-router
        # edges (`_CallableRouter` discriminator). Empty at startup;
        # operators register callables via a startup hook or a future
        # POST /v1/_graph_routers/{id} endpoint. Graphs with only
        # static + json_path edges work fine without any registration.
        from matrix.graph.router import RouterRegistry

        router_registry = RouterRegistry()
        app.state.router_registry = router_registry

        # --- Scheduler + worker pool wiring (Task 23) ----------------
        scheduler = None
        if scheduler_config is not None:
            from matrix.scheduler.factory import SchedulerFactory

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
        yield_listener = None
        timer_scheduler = None
        timeout_sweeper = None
        watcher_manager = None
        mcp_task_bridge = None
        if scheduler is not None:
            from matrix.bus.in_memory import InMemoryEventBus
            from matrix.bus.listener import YieldEventListener
            from matrix.bus.mcp_tasks import McpTaskBridge
            from matrix.bus.postgres import PostgresEventBus
            from matrix.bus.scheduler_tasks import (
                TimeoutSweeper, TimerScheduler,
            )
            from matrix.bus.watcher import WatcherManager
            from matrix.scheduler.postgres import PostgresScheduler

            # Pair the bus to the scheduler flavour: postgres scheduler
            # → LISTEN/NOTIFY bus (cross-app delivery); in-memory
            # scheduler → in-process bus.
            if isinstance(scheduler, PostgresScheduler):
                event_bus = PostgresEventBus(scheduler._storage)
            else:
                event_bus = InMemoryEventBus()
            logger.info("lifespan: event bus initialise")
            await event_bus.initialize()
            logger.info("lifespan: starting yield listener / timer / sweeper")
            yield_listener = YieldEventListener(
                bus=event_bus, scheduler=scheduler,
            )
            yield_listener.start()
            timer_scheduler = TimerScheduler(
                bus=event_bus, scheduler=scheduler,
            )
            timer_scheduler.start()
            timeout_sweeper = TimeoutSweeper(
                bus=event_bus, scheduler=scheduler,
            )
            timeout_sweeper.start()

            # watch_files watcher manager — resolves workspace_id →
            # filesystem root via the workspace registry. Returns None
            # for non-local backends (sandbox/container/k8s) so the
            # manager logs a warning and skips them; native watcher
            # support for those backends is future work.
            async def _resolve_root(workspace_id: str):
                try:
                    ws = await workspace_registry.get_workspace(workspace_id)
                except Exception:
                    return None
                root = getattr(ws, "root", None)
                return root
            watcher_manager = WatcherManager(
                bus=event_bus,
                scheduler=scheduler,
                workspace_root_resolver=_resolve_root,
            )
            watcher_manager.start()
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
            mcp_task_bridge.start()
            logger.info("lifespan: mcp task bridge started")
        app.state.event_bus = event_bus

        worker_pool = None
        if config.runtime_mode in (
            RuntimeMode.WORKER, RuntimeMode.API_PLUS_WORKER,
        ):
            from matrix.worker.pool import WorkerPool

            worker_pool = WorkerPool(
                config=config.worker,
                scheduler=scheduler,
                storage=storage_provider,
                workspace_registry=workspace_registry,
                provider_registry=provider_registry,
                router_registry=router_registry,
                approval_resolver=approval_resolver,
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
        if ic_config is not None:
            ic_subsystem = build_subsystem(
                config=ic_config,
                storage_provider=storage_provider,
                provider_registry=provider_registry,
                semantic_search_registry=semantic_search_registry,
                toolset_providers={
                    "_system": system_toolset,
                    "_workspaces": ws_toolset,
                    "_misc": misc_toolset,
                },
            )
            search_toolset = build_search_toolset(ic_subsystem)
            ic_subsystem.register_toolset_provider("_search", search_toolset)
            provider_registry._search_toolset_provider = search_toolset  # noqa: SLF001
            app.state.internal_collections = ic_subsystem
            app.state.search_toolset = search_toolset
            ic_subsystem.start_worker()
        logger.info(
            "matrix API ready",
            extra={"version": APP_VERSION, "host": config.host, "port": config.port},
        )
        try:
            yield
        finally:
            # Order matters: drain the pool first so in-flight turns get
            # a chance to settle while the scheduler is still alive,
            # then close the scheduler, then the rest of the
            # subsystems. Each step is guarded so a teardown failure
            # downstream still runs the others.
            if worker_pool is not None:
                try:
                    await worker_pool.drain_and_stop()
                except Exception:
                    logger.exception("worker_pool.drain_and_stop failed")
            # Stop yield background tasks BEFORE the scheduler / bus
            # close so an in-flight tick doesn't race a closing bus.
            for task, name in (
                (mcp_task_bridge, "mcp_task_bridge"),
                (watcher_manager, "watcher_manager"),
                (timeout_sweeper, "timeout_sweeper"),
                (timer_scheduler, "timer_scheduler"),
                (yield_listener, "yield_listener"),
            ):
                if task is not None:
                    try:
                        await task.stop()
                    except Exception:
                        logger.exception("%s.stop failed", name)
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
                await provider_registry.aclose()
            except Exception:
                logger.exception("provider_registry.aclose failed")
            try:
                await semantic_search_registry.aclose()
            except Exception:
                logger.exception("semantic_search_registry.aclose failed")
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
    ``~/.matrix/db/data.sqlite``. The parent directory is created
    on demand inside :meth:`SqliteStorageProvider.initialize`.
    """
    from matrix.model.provider import (
        SqliteConfig as _SqliteConfig,
        StorageProviderConfig as _StorageProviderConfig,
        StorageProviderType as _StorageProviderType,
    )
    from matrix.storage.factory import StorageProviderFactory

    sp_config = config.db
    if sp_config is None:
        default_path = Path.home() / ".matrix" / "db" / "data.sqlite"
        sp_config = _StorageProviderConfig(
            provider=_StorageProviderType.SQLITE,
            config=_SqliteConfig(path=default_path),
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
    prefix = f"/{API_VERSION}"
    # Always-on routers — health probes + worker observability/drain.
    app.include_router(health.router, prefix=prefix)
    app.include_router(workers_router.router, prefix=prefix)
    if runtime_mode == RuntimeMode.WORKER:
        return
    # Phase 1 — providers + tools
    app.include_router(providers.llm_provider_router, prefix=prefix)
    app.include_router(providers.embedding_provider_router, prefix=prefix)
    app.include_router(providers.cross_encoder_provider_router, prefix=prefix)
    app.include_router(providers.toolset_router, prefix=prefix)
    app.include_router(semantic_search_router, prefix=prefix)
    # Phase 2 — compute (Agent + Graph)
    app.include_router(compute.agent_router, prefix=prefix)
    app.include_router(compute.graph_router, prefix=prefix)
    # Phase 3 — knowledge (Collection + Document). VectorStoreConfig
    # has been removed; vector store is now managed per-row via
    # SemanticSearchProvider rows (SSP registry).
    app.include_router(knowledge.collection_router, prefix=prefix)
    app.include_router(knowledge.document_router, prefix=prefix)
    # Internal collections subsystem (config + bootstrap + per-entity
    # semantic search). The search routes return 503 until the
    # subsystem has been bootstrapped at least once.
    app.include_router(internal_collections.router, prefix=prefix)
    # Workspaces (providers, templates, workspaces + sessions / files /
    # log sub-resources). Bespoke create/delete; PUT only for templates.
    app.include_router(workspaces_router.provider_router, prefix=prefix)
    app.include_router(workspaces_router.template_router, prefix=prefix)
    app.include_router(workspaces_router.workspace_router, prefix=prefix)
    app.include_router(workspaces_router.sessions_router, prefix=prefix)
    app.include_router(workspaces_router.files_router, prefix=prefix)
    app.include_router(workspaces_router.log_router, prefix=prefix)
    # Sessions: nested CREATE under /v1/workspaces/{wid}/sessions plus
    # the (currently empty) top-level router. Task 20 fills the top
    # router with cross-workspace list/get/find and the resume / pause /
    # cancel sub-resources.
    app.include_router(sessions_router.nested_session_router, prefix=prefix)
    app.include_router(sessions_router.top_session_router, prefix=prefix)
    # Yielding-tools surface (M3+): ask_user pending/respond + the
    # tool-agnostic cancel-yielded-tool. Routes live under
    # /v1/sessions/{id}/...; the lifespan handler attaches the event
    # bus required by the publish path.
    app.include_router(yields_router.yields_router, prefix=prefix)
    # Chat surface (M6): REST + WS for agent-driven conversations.
    # Park fields on the Chat row reuse the M1-M5 yield machinery so
    # the same listener / timer / sweeper / watcher / mcp-bridge
    # wakes parked chats just like parked sessions.
    app.include_router(chats_router.chats_router, prefix=prefix)
    # Tool approval policies (§2 task 5): CRUD + invalidate endpoint.
    from matrix.api.routers.tool_approval import make_tool_approval_router
    app.include_router(make_tool_approval_router(), prefix=prefix)


def create_app(config: AppConfig) -> FastAPI:
    """Production factory: builds the app + wires the lifespan handler."""
    # Disable Swagger / ReDoc UIs unless the operator opts back in via
    # the log_level=debug setting; the OpenAPI JSON stays under the
    # /v1/ prefix to match the rest of the versioned API surface.
    debug_docs = config.log_level == "debug"
    app = FastAPI(
        title="Matrix Microagents Framework API",
        version=APP_VERSION,
        lifespan=_make_lifespan(config),
        contact={"name": "matrix"},
        openapi_url=f"/{API_VERSION}/openapi.json",
        docs_url=f"/{API_VERSION}/docs" if debug_docs else None,
        redoc_url=f"/{API_VERSION}/redoc" if debug_docs else None,
    )
    _install_security_headers(app)
    _install_console_csp(app)
    _install_request_id(app)
    _mount_routers(app, runtime_mode=config.runtime_mode)
    _mount_console(app)
    _install_root_redirect(app)
    register_error_handlers(app)
    return app


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
    "script-src 'self' 'unsafe-eval' 'unsafe-inline' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
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
            # Force revalidation on every request. The static-file
            # mount sends ETags so the browser usually gets a cheap
            # 304; this header makes it actually ask. Prevents the
            # classic "I edited styles.css / *.jsx and the browser
            # is still serving last week's copy" trap. Spec §13 open
            # question #8 (static-asset versioning) is addressed here
            # for the dev/operator surface; production CDN caching
            # would need a separate strategy.
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
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
            StaticFiles(directory=str(_UI_DIR), html=True),
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
) -> FastAPI:
    """Test factory: skips the lifespan; stashes pre-built dependencies.

    If any of ``system_toolset``, ``workspace_registry``,
    ``workspaces_toolset``, or ``misc_toolset`` is omitted the factory
    builds one against the supplied registries — the same wiring the
    production lifespan performs. Pass an explicit instance to inject
    a stub.
    """
    app = FastAPI(
        title="Matrix Microagents Framework API (test)",
        version=APP_VERSION,
        contact={"name": "matrix"},
    )
    _install_request_id(app)
    if workspace_registry is None:
        workspace_registry = WorkspaceRegistry(storage_provider)
    # Wire the SemanticSearchRegistry so /v1/ssp endpoints work in tests.
    from matrix.model.provider import SemanticSearchProvider
    _test_ssp_registry = SemanticSearchRegistry(
        storage=storage_provider.get_storage(SemanticSearchProvider),
        factory=lambda row: object(),  # type: ignore[arg-type]
    )
    from matrix.agent.approval import ApprovalResolver as _AR
    from matrix.model.tool_approval import ToolApprovalPolicy as _TAP
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
        workspaces_toolset = build_workspaces_toolset(
            storage_provider=storage_provider,
            workspace_registry=workspace_registry,
        )
    if misc_toolset is None:
        misc_toolset = build_misc_toolset()
    if web_toolset is None:
        web_toolset = build_web_toolset()
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
    # Tests build the subsystem on demand via the /bootstrap endpoint.
    app.state.internal_collections = None
    app.state.search_toolset = None
    # Attach an in-memory scheduler so the /workers router has something
    # to depend on. The test app does not run a real WorkerPool.
    from matrix.scheduler.in_memory import InMemoryScheduler
    app.state.scheduler = InMemoryScheduler()
    app.state.worker_pool = None
    _mount_routers(app)
    register_error_handlers(app)
    return app


__all__ = ["create_app", "create_test_app"]
