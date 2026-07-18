"""FastAPI app factory + lifespan handler.

This module is a thin facade over the focused submodules the app was
split into for maintainability:

* :mod:`primer.api._app_bootstrap` - first-boot bootstrap helpers +
  storage-provider construction.
* :mod:`primer.api._app_lifespan` - the production lifespan handler.
* :mod:`primer.api._app_routes` - router registration.
* :mod:`primer.api._app_middleware` - middleware, static mounts, and
  route-installer helpers.
* :mod:`primer.api._app_mcp` - the /v1/mcp mount + auth gate.

The factories (:func:`create_app`, :func:`create_test_app`) live here.
Every symbol previously importable from ``primer.api.app`` is re-exported
below so external call sites keep working unchanged.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from fastapi import FastAPI

from primer.api.config import AppConfig
from primer.api.errors import register_error_handlers
from primer.api.registries import (
    ProviderRegistry,
    SemanticSearchRegistry,
    WorkspaceRegistry,
)
from primer.api.version import API_VERSION, APP_VERSION

# Importing these modules registers each channel adapter factory with
# primer.channel.factory. Each platform SDK ships in the optional
# ``channels`` extra, so a slim install may not have it; when a platform
# package is absent we skip registering it, and ``build_adapter`` raises a
# clear ConfigError if a Channel row later names that uninstalled provider.
for _channel_factory_mod in (
    "primer.channel.slack.factory",
    "primer.channel.telegram.factory",
    "primer.channel.discord.factory",
):
    try:
        importlib.import_module(_channel_factory_mod)
    except ModuleNotFoundError:
        logging.getLogger(__name__).debug(
            "channel platform not installed; skipping registration: %s",
            _channel_factory_mod,
        )
from primer.model.scheduler import RuntimeMode
from primer.toolset.misc import build_misc_toolset
from primer.toolset.system import build_system_toolset
from primer.toolset.workspace_ext import build_workspace_ext_toolset
from primer.toolset.web import build_web_toolset
from primer.toolset.workspaces import build_workspaces_toolset

# --- Facade re-exports -------------------------------------------------
# Everything below was previously defined inline in this module. The
# definitions moved into focused submodules; re-exporting them here keeps
# the public ``from primer.api.app import X`` surface byte-stable.
from primer.api._app_bootstrap import (
    _bootstrap_web_fetch,
    _bootstrap_web_search,
    _build_storage_provider,
)
from primer.api._app_lifespan import _make_lifespan
from primer.api._app_mcp import (
    _make_mcp_auth_gate,
    _mcp_send_simple_response,
    _start_mcp_mount,
)
from primer.api._app_middleware import (
    _CachingStaticFiles,
    _CONSOLE_CSP,
    _GZipExceptMcp,
    _UI_DIR,
    _resolve_ui_dir,
    _install_auth_middleware,
    _install_console_csp,
    _install_jsx_bundle,
    _install_request_id,
    _install_root_redirect,
    _install_security_headers,
    _mount_console,
    _mount_metrics,
)
from primer.api._app_routes import _mount_routers


if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)


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
    _install_jsx_bundle(app, docs_url=config.docs_url)
    _mount_console(app)
    _mount_metrics(app, config)
    _install_root_redirect(app)
    register_error_handlers(app)
    return app


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
    # secret_provider omitted: this lightweight app path does not wire
    # secret-sourced file mounts (they raise a clear error if declared).
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
            workspace_registry=workspace_registry,
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
            workspace_registry=workspace_registry,
        )
    # Always-on workspace_ext toolset (workspace-only yielding tools).
    workspace_ext_toolset = build_workspace_ext_toolset(
        storage_provider=storage_provider,
    )
    provider_registry._system_toolset_provider = system_toolset  # noqa: SLF001
    provider_registry._workspaces_toolset_provider = workspaces_toolset  # noqa: SLF001
    provider_registry._misc_toolset_provider = misc_toolset  # noqa: SLF001
    provider_registry._web_toolset_provider = web_toolset  # noqa: SLF001
    provider_registry._workspace_ext_toolset_provider = (  # noqa: SLF001
        workspace_ext_toolset
    )
    app.state.storage_provider = storage_provider
    app.state.provider_registry = provider_registry
    app.state.workspace_registry = workspace_registry
    app.state.system_toolset = system_toolset
    app.state.workspaces_toolset = workspaces_toolset
    app.state.misc_toolset = misc_toolset
    app.state.web_toolset = web_toolset
    app.state.workspace_ext_toolset = workspace_ext_toolset
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

        app.router.lifespan_context = _test_lifespan  # type: ignore[assignment]

    return app


__all__ = ["create_app", "create_test_app"]
