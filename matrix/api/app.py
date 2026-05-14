"""FastAPI app factory + lifespan handler."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from matrix.api.config import AppConfig
from matrix.api.errors import register_error_handlers
from matrix.api.registries import (
    ProviderRegistry,
    VectorStoreRegistry,
    WorkspaceRegistry,
)
from matrix.api.routers import (
    compute,
    health,
    internal_collections,
    knowledge,
    providers,
    sessions as sessions_router,
    workers as workers_router,
    workspaces as workspaces_router,
)
from matrix.api.version import API_VERSION, APP_VERSION
from matrix.internal_collections import build_subsystem, load_config_or_none
from matrix.model.except_ import ConfigError
from matrix.model.scheduler import RuntimeMode, SchedulerProviderType
from matrix.toolset.misc import build_misc_toolset
from matrix.toolset.search import build_search_toolset
from matrix.toolset.system import build_system_toolset
from matrix.toolset.workspaces import build_workspaces_toolset


if TYPE_CHECKING:
    from matrix.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)


def _make_lifespan(config: AppConfig):
    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Fail fast on a worker mode without a scheduler — saves spinning
        # up the storage pool just to tear it back down. Task 23.
        if config.runtime_mode in (
            RuntimeMode.WORKER, RuntimeMode.API_PLUS_WORKER,
        ) and config.scheduler is None:
            raise ConfigError(
                f"runtime_mode={config.runtime_mode.value!r} requires "
                "scheduler config; set MATRIX_SCHEDULER__PROVIDER or "
                "configure via TOML."
            )

        storage_provider = _build_storage_provider(config)
        await storage_provider.initialize()
        vector_store_registry = VectorStoreRegistry(config.vector_store)
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
            vector_store_registry=vector_store_registry,
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
        app.state.storage_provider = storage_provider
        app.state.provider_registry = provider_registry
        app.state.vector_store_registry = vector_store_registry
        app.state.workspace_registry = workspace_registry
        app.state.system_toolset = system_toolset
        app.state.workspaces_toolset = ws_toolset
        app.state.misc_toolset = misc_toolset
        app.state.internal_collections = None
        app.state.search_toolset = None

        # --- Scheduler + worker pool wiring (Task 23) ----------------
        scheduler = None
        if config.scheduler is not None:
            from matrix.scheduler.factory import SchedulerFactory

            scheduler = SchedulerFactory.create(
                config.scheduler, storage_provider=storage_provider,
            )
            await scheduler.initialize()
            # Loud warning: in-memory scheduler is single-process; running it
            # alongside any worker pool (whether colocated 'api+worker' or
            # separate 'worker' processes) means cross-process state is not
            # synchronised — sessions can be double-claimed. Production should
            # use the Postgres scheduler. See spec §9.1.
            if (
                config.scheduler.provider == SchedulerProviderType.IN_MEMORY
                and config.runtime_mode != RuntimeMode.API
            ):
                logger.warning(
                    "in-memory scheduler with runtime_mode=%s is not safe for "
                    "multi-worker deployment; switch to Postgres for production",
                    config.runtime_mode.value,
                )
        app.state.scheduler = scheduler

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
            )
            await worker_pool.start()
        app.state.worker_pool = worker_pool

        # Internal collections subsystem auto-activation: if a config
        # row already exists in storage, build the live subsystem +
        # search toolset and start the CDC worker. We do NOT auto-run
        # bootstrap here — the operator does that explicitly via
        # POST /v1/internal_collections/bootstrap.
        ic_config = await load_config_or_none(storage_provider)
        if ic_config is not None:
            ic_subsystem = build_subsystem(
                config=ic_config,
                storage_provider=storage_provider,
                provider_registry=provider_registry,
                vector_store_registry=vector_store_registry,
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
                await vector_store_registry.aclose()
            except Exception:
                logger.exception("vector_store_registry.aclose failed")
            try:
                await workspace_registry.aclose()
            except Exception:
                logger.exception("workspace_registry.aclose failed")
            try:
                await storage_provider.aclose()
            except Exception:
                logger.exception("storage_provider.aclose failed")

    return _lifespan


def _build_storage_provider(config: AppConfig) -> "StorageProvider":  # pragma: no cover
    """Construct the Postgres storage provider from the AppConfig.

    Marked no-cover because the production path requires a live
    Postgres; tests monkeypatch this function with a fake
    StorageProvider for the lifespan-handler test in test_app_factory.
    """
    from matrix.model.provider import (
        PoolConfig,
        PostgresConfig,
        StorageProviderConfig,
        StorageProviderType,
    )
    from matrix.storage.factory import StorageProviderFactory

    sp_config = StorageProviderConfig(
        provider=StorageProviderType.POSTGRES,
        config=PostgresConfig(
            hostname=config.db_host,
            port=config.db_port,
            database=config.db_database,
            username=config.db_user,
            password=config.db_password,
            pool=PoolConfig(
                min_size=config.db_min_pool_size,
                max_size=config.db_max_pool_size,
            ),
        ),
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
    # Phase 2 — compute (Agent + Graph)
    app.include_router(compute.agent_router, prefix=prefix)
    app.include_router(compute.graph_router, prefix=prefix)
    # Phase 3 — knowledge (Collection + Document). VectorStoreConfig
    # has moved out of storage and into AppConfig.vector_store; no
    # CRUD endpoint exists for it any more.
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


def create_app(config: AppConfig) -> FastAPI:
    """Production factory: builds the app + wires the lifespan handler."""
    # Disable Swagger / ReDoc UIs unless the operator opts back in via
    # the log_level=debug setting; the OpenAPI JSON stays available at
    # /openapi.json for client-generation pipelines.
    debug_docs = config.log_level == "debug"
    app = FastAPI(
        title="Matrix Microagents Framework API",
        version=APP_VERSION,
        lifespan=_make_lifespan(config),
        contact={"name": "matrix"},
        docs_url="/docs" if debug_docs else None,
        redoc_url="/redoc" if debug_docs else None,
    )
    _install_security_headers(app)
    _mount_routers(app, runtime_mode=config.runtime_mode)
    register_error_handlers(app)
    return app


def _install_security_headers(app: FastAPI) -> None:
    """Set conservative defensive headers on every response.

    The API is JSON-only, so a strict ``no-sniff`` + deny-frame policy
    is safe by default. ``Cross-Origin-Resource-Policy: same-origin``
    blocks no-CORS embeds from other origins. CSP is omitted (it has
    no effect on JSON responses, and configuring it for the optional
    debug-mode Swagger UI would add a maintenance surface).
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


def create_test_app(
    *,
    storage_provider: "StorageProvider",
    provider_registry: ProviderRegistry,
    vector_store_registry: VectorStoreRegistry,
    workspace_registry: WorkspaceRegistry | None = None,
    system_toolset=None,
    workspaces_toolset=None,
    misc_toolset=None,
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
    if workspace_registry is None:
        workspace_registry = WorkspaceRegistry(storage_provider)
    if system_toolset is None:
        system_toolset = build_system_toolset(
            storage_provider=storage_provider,
            provider_registry=provider_registry,
            vector_store_registry=vector_store_registry,
        )
    if workspaces_toolset is None:
        workspaces_toolset = build_workspaces_toolset(
            storage_provider=storage_provider,
            workspace_registry=workspace_registry,
        )
    if misc_toolset is None:
        misc_toolset = build_misc_toolset()
    provider_registry._system_toolset_provider = system_toolset  # noqa: SLF001
    provider_registry._workspaces_toolset_provider = workspaces_toolset  # noqa: SLF001
    provider_registry._misc_toolset_provider = misc_toolset  # noqa: SLF001
    app.state.storage_provider = storage_provider
    app.state.provider_registry = provider_registry
    app.state.vector_store_registry = vector_store_registry
    app.state.workspace_registry = workspace_registry
    app.state.system_toolset = system_toolset
    app.state.workspaces_toolset = workspaces_toolset
    app.state.misc_toolset = misc_toolset
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
