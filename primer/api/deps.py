"""FastAPI ``Depends`` helpers.

Three layers:

1. Singleton resolvers that read pre-built dependencies from
   ``app.state``.
2. Per-model ``Storage[T]`` resolvers that use the
   :class:`StorageProvider` to fetch the right typed handle.
3. Principal passthrough that pulls the optional
   ``X-Primer-Principal`` request header.

The lifespan handler (or test factory) MUST stash two attributes on
``app.state`` before the first request: ``storage_provider`` and
``provider_registry``. Each resolver defends against missing state by
raising ``ConfigError``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, Header, HTTPException, Request

from primer.api.registries import (
    ProviderRegistry,
    WorkspaceRegistry,
)
from primer.model.agent import Agent
from primer.model.collection import Collection, Document
from primer.model.except_ import ConfigError
from primer.model.graph import Graph
from primer.model.internal import IngestFailure, InternalCollectionsConfig
from primer.model.provider import (
    CrossEncoderProvider,
    EmbeddingProvider,
    LLMProvider,
    Toolset,
)
from primer.model.workspace_session import WorkspaceSession
from primer.model.workspace import (
    Workspace,
    WorkspaceProvider,
    WorkspaceTemplate,
)


if TYPE_CHECKING:
    from primer.int.claim import ClaimEngine
    from primer.int.event_bus import EventBus
    from primer.int.scheduler import Scheduler
    from primer.int.storage import Storage
    from primer.int.storage_provider import StorageProvider
    from primer.worker.pool import WorkerPool


PRINCIPAL_HEADER = "X-Primer-Principal"


def _assert_app_state_initialized(request: Request) -> None:
    state = request.app.state
    missing = [
        name
        for name in ("storage_provider", "provider_registry")
        if not hasattr(state, name) or getattr(state, name) is None
    ]
    if missing:
        raise ConfigError(
            f"API state not initialised; missing attributes on app.state: "
            f"{', '.join(missing)}. The lifespan handler (or "
            "create_test_app) must set storage_provider and "
            "provider_registry before any request is served."
        )


def get_storage_provider(request: Request) -> "StorageProvider":
    _assert_app_state_initialized(request)
    return request.app.state.storage_provider


def get_provider_registry(request: Request) -> ProviderRegistry:
    _assert_app_state_initialized(request)
    return request.app.state.provider_registry


def get_semantic_search_registry(request: Request) -> "SemanticSearchRegistry":
    """Return the per-process SemanticSearchRegistry stored on app.state.

    Raises 503 ``/errors/subsystem-inactive`` if the registry wasn't
    constructed at lifespan time (test paths that skip registry wiring).
    """
    from primer.api.registries.semantic_search_registry import (
        SemanticSearchRegistry,
    )
    reg = getattr(request.app.state, "semantic_search_registry", None)
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "type": "/errors/subsystem-inactive",
                "title": "SemanticSearch registry not configured",
            },
        )
    return reg


def get_semantic_search_storage(
    storage_provider=Depends(get_storage_provider),
) -> "Storage":
    from primer.model.provider import SemanticSearchProvider
    return storage_provider.get_storage(SemanticSearchProvider)


def get_workspace_registry(request: Request) -> WorkspaceRegistry:
    """Resolve the live :class:`WorkspaceRegistry`.

    Stashed by the lifespan handler / ``create_test_app`` alongside the
    other registries on ``app.state``.
    """
    _assert_app_state_initialized(request)
    registry = getattr(request.app.state, "workspace_registry", None)
    if registry is None:
        raise ConfigError(
            "WorkspaceRegistry not on app.state — lifespan handler "
            "(or create_test_app) must build one."
        )
    return registry


def get_llm_provider_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
) -> "Storage[LLMProvider]":
    return sp.get_storage(LLMProvider)


def get_embedding_provider_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
) -> "Storage[EmbeddingProvider]":
    return sp.get_storage(EmbeddingProvider)


def get_cross_encoder_provider_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
) -> "Storage[CrossEncoderProvider]":
    return sp.get_storage(CrossEncoderProvider)


def get_toolset_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
) -> "Storage[Toolset]":
    return sp.get_storage(Toolset)


def get_agent_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
) -> "Storage[Agent]":
    return sp.get_storage(Agent)


def get_graph_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
) -> "Storage[Graph]":
    return sp.get_storage(Graph)


def get_collection_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
) -> "Storage[Collection]":
    return sp.get_storage(Collection)


def get_document_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
) -> "Storage[Document]":
    return sp.get_storage(Document)


def get_workspace_provider_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
) -> "Storage[WorkspaceProvider]":
    return sp.get_storage(WorkspaceProvider)


def get_workspace_template_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
) -> "Storage[WorkspaceTemplate]":
    return sp.get_storage(WorkspaceTemplate)


def get_workspace_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
) -> "Storage[Workspace]":
    return sp.get_storage(Workspace)


def get_session_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
) -> "Storage[WorkspaceSession]":
    return sp.get_storage(WorkspaceSession)


def get_chat_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
):
    from primer.model.chats import Chat
    return sp.get_storage(Chat)


def get_internal_collections_config_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
) -> "Storage[InternalCollectionsConfig]":
    return sp.get_storage(InternalCollectionsConfig)


def get_ingest_failure_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
) -> "Storage[IngestFailure]":
    return sp.get_storage(IngestFailure)


def get_internal_collections_subsystem(request: Request):
    """Resolve the live :class:`InternalCollectionsSubsystem`.

    Returns the subsystem instance attached to ``app.state`` by the
    lifespan handler (or ``create_test_app``). Raises
    :class:`ConfigError` when the subsystem isn't on the app — that
    happens when the lifespan ran without a config row and the
    subsystem hasn't been activated via the API yet.
    """
    _assert_app_state_initialized(request)
    subsystem = getattr(request.app.state, "internal_collections", None)
    if subsystem is None:
        raise ConfigError(
            "internal collections subsystem is not active; configure "
            "it via PUT /v1/internal_collections/config and run "
            "POST /v1/internal_collections/bootstrap."
        )
    return subsystem


def get_scheduler(request: Request) -> "Scheduler":
    """Resolve the live :class:`Scheduler`.

    Stashed by the lifespan handler / ``create_test_app`` on
    ``app.state.scheduler``. Raises :class:`ConfigError` when absent.
    """
    sched = getattr(request.app.state, "scheduler", None)
    if sched is None:
        raise ConfigError(
            "Scheduler not on app.state — configure scheduler in "
            "AppConfig and re-launch."
        )
    return sched


def get_event_bus(request: Request) -> "EventBus":
    """Resolve the live :class:`EventBus`.

    Stashed by the lifespan handler (or test fixture) on
    ``app.state.event_bus``. The yielding-tool endpoints publish
    operator responses + cancel markers through it; the listener
    subscribes and flips parked rows to resumable. Raises
    :class:`ConfigError` when absent — a deployment that wants the
    yielding-tool API must wire a bus.
    """
    bus = getattr(request.app.state, "event_bus", None)
    if bus is None:
        raise ConfigError(
            "EventBus not on app.state — the yielding-tool feature "
            "requires a bus. The production lifespan builds one; tests "
            "that hit /v1/sessions/.../ask_user must attach an "
            "InMemoryEventBus to app.state.event_bus."
        )
    return bus


def get_worker_pool(request: Request) -> "WorkerPool":
    """Resolve the live :class:`WorkerPool`.

    Stashed by the lifespan handler when ``runtime_mode`` includes a
    worker. Raises :class:`ConfigError` when absent.
    """
    pool = getattr(request.app.state, "worker_pool", None)
    if pool is None:
        raise ConfigError(
            "WorkerPool not on app.state — set runtime_mode to "
            "'worker' or 'api+worker' to start it."
        )
    return pool


def get_approval_resolver(request: Request):
    """Dependency: the singleton ApprovalResolver wired in lifespan."""
    resolver = getattr(request.app.state, "approval_resolver", None)
    if resolver is None:
        raise ConfigError(
            "approval_resolver not initialised on app.state"
        )
    return resolver


def get_channel_registry(request: Request):
    reg = getattr(request.app.state, "channel_registry", None)
    if reg is None:
        raise ConfigError("channel_registry not initialised")
    return reg


def get_channel_dispatcher(request: Request):
    d = getattr(request.app.state, "channel_dispatcher", None)
    if d is None:
        raise ConfigError("channel_dispatcher not initialised")
    return d


def get_channel_inbox(request: Request):
    i = getattr(request.app.state, "channel_inbox", None)
    if i is None:
        raise ConfigError("channel_inbox not initialised")
    return i


def get_claim_engine(request: Request) -> "ClaimEngine | None":
    """Return the live :class:`ClaimEngine` stored on ``app.state``.

    Returns ``None`` when the engine hasn't been wired yet (Tasks 1-16
    run without it; Task 17 lights it up in the lifespan). Callers MUST
    treat ``None`` as a no-op — do not call engine methods on ``None``.
    """
    return getattr(request.app.state, "claim_engine", None)


def get_principal(
    x_primer_principal: str | None = Header(default=None, alias=PRINCIPAL_HEADER),
) -> str | None:
    """Per-request end-user identity. ``None`` if header absent."""
    return x_primer_principal


__all__ = [
    "PRINCIPAL_HEADER",
    "get_approval_resolver",
    "get_agent_storage",
    "get_channel_dispatcher",
    "get_channel_inbox",
    "get_channel_registry",
    "get_chat_storage",
    "get_claim_engine",
    "get_collection_storage",
    "get_cross_encoder_provider_storage",
    "get_document_storage",
    "get_embedding_provider_storage",
    "get_event_bus",
    "get_graph_storage",
    "get_ingest_failure_storage",
    "get_internal_collections_config_storage",
    "get_internal_collections_subsystem",
    "get_llm_provider_storage",
    "get_principal",
    "get_provider_registry",
    "get_scheduler",
    "get_semantic_search_registry",
    "get_semantic_search_storage",
    "get_session_storage",
    "get_storage_provider",
    "get_toolset_storage",
    "get_worker_pool",
    "get_workspace_provider_storage",
    "get_workspace_registry",
    "get_workspace_storage",
    "get_workspace_template_storage",
]
