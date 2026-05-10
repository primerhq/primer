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
    workspaces as workspaces_router,
)
from matrix.api.version import API_VERSION, APP_VERSION
from matrix.internal_collections import build_subsystem, load_config_or_none
from matrix.toolset.search import build_search_toolset
from matrix.toolset.system import build_system_toolset
from matrix.toolset.workspaces import build_workspaces_toolset


if TYPE_CHECKING:
    from matrix.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)


def _make_lifespan(config: AppConfig):
    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        storage_provider = _build_storage_provider(config)
        await storage_provider.initialize()
        vector_store_registry = VectorStoreRegistry(config.vector_store)
        workspace_registry = WorkspaceRegistry(storage_provider)
        # Bootstrap the system toolset before constructing the
        # ProviderRegistry so the registry can short-circuit
        # ``get_toolset('_system')`` to it.
        provider_registry = ProviderRegistry(storage_provider)
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
        app.state.storage_provider = storage_provider
        app.state.provider_registry = provider_registry
        app.state.vector_store_registry = vector_store_registry
        app.state.workspace_registry = workspace_registry
        app.state.system_toolset = system_toolset
        app.state.workspaces_toolset = ws_toolset
        app.state.internal_collections = None
        app.state.search_toolset = None
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
            ic_subsystem = app.state.internal_collections
            if ic_subsystem is not None:
                await ic_subsystem.aclose()
            await provider_registry.aclose()
            await vector_store_registry.aclose()
            await workspace_registry.aclose()
            await storage_provider.aclose()

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


def _mount_routers(app: FastAPI) -> None:
    """Mount every router under the API version prefix."""
    prefix = f"/{API_VERSION}"
    app.include_router(health.router, prefix=prefix)
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


def create_app(config: AppConfig) -> FastAPI:
    """Production factory: builds the app + wires the lifespan handler."""
    app = FastAPI(
        title="Matrix Microagents Framework API",
        version=APP_VERSION,
        lifespan=_make_lifespan(config),
        contact={"name": "matrix"},
    )
    _mount_routers(app)
    register_error_handlers(app)
    return app


def create_test_app(
    *,
    storage_provider: "StorageProvider",
    provider_registry: ProviderRegistry,
    vector_store_registry: VectorStoreRegistry,
    workspace_registry: WorkspaceRegistry | None = None,
    system_toolset=None,
    workspaces_toolset=None,
) -> FastAPI:
    """Test factory: skips the lifespan; stashes pre-built dependencies.

    If any of ``system_toolset``, ``workspace_registry``, or
    ``workspaces_toolset`` is omitted the factory builds one against
    the supplied registries — the same wiring the production lifespan
    performs. Pass an explicit instance to inject a stub.
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
    provider_registry._system_toolset_provider = system_toolset  # noqa: SLF001
    provider_registry._workspaces_toolset_provider = workspaces_toolset  # noqa: SLF001
    app.state.storage_provider = storage_provider
    app.state.provider_registry = provider_registry
    app.state.vector_store_registry = vector_store_registry
    app.state.workspace_registry = workspace_registry
    app.state.system_toolset = system_toolset
    app.state.workspaces_toolset = workspaces_toolset
    # Tests build the subsystem on demand via the /bootstrap endpoint.
    app.state.internal_collections = None
    app.state.search_toolset = None
    _mount_routers(app)
    register_error_handlers(app)
    return app


__all__ = ["create_app", "create_test_app"]
