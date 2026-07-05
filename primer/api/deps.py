"""FastAPI ``Depends`` helpers.

Three layers:

1. Singleton resolvers that read pre-built dependencies from
   ``app.state``.
2. Per-model ``Storage[T]`` resolvers that use the
   :class:`StorageProvider` to fetch the right typed handle.
3. Principal passthrough that reads from ``request.state`` (populated
   by :class:`primer.api.middleware.auth.AuthMiddleware` after
   verifying the signed ``primer_session`` cookie).

The lifespan handler (or test factory) MUST stash two attributes on
``app.state`` before the first request: ``storage_provider`` and
``provider_registry``. Each resolver defends against missing state by
raising ``ConfigError``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Request

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
from primer.model.user import User
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


def get_artifact_storage_registry(request: Request):
    """Return the per-process ArtifactStorageRegistry stored on app.state."""
    reg = getattr(request.app.state, "artifact_storage_registry", None)
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "type": "/errors/subsystem-inactive",
                "title": "Artifact storage registry not configured",
            },
        )
    return reg


def get_artifact_storage_provider_storage(
    storage_provider=Depends(get_storage_provider),
) -> "Storage":
    from primer.model.provider import ArtifactStorageProvider
    return storage_provider.get_storage(ArtifactStorageProvider)


def get_oidc_provider_storage(
    storage_provider=Depends(get_storage_provider),
) -> "Storage":
    """Typed :class:`Storage` handle for :class:`OidcProvider` rows.

    Mirrors :func:`get_artifact_storage_provider_storage`. Consumed by
    the admin OIDC-providers CRUD router.
    """
    from primer.model.oidc import OidcProvider
    return storage_provider.get_storage(OidcProvider)


def get_user_identity_storage(
    storage_provider: "StorageProvider" = Depends(get_storage_provider),
) -> "Storage":
    """Typed :class:`Storage` handle for :class:`UserIdentity` rows.

    Mirrors :func:`get_oidc_provider_storage`. Consumed by the SSO
    callback route to resolve (or JIT-provision) the local account
    bound to an OIDC ``(provider_id, sub)`` pair.
    """
    from primer.model.oidc import UserIdentity
    return storage_provider.get_storage(UserIdentity)


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


def get_user_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
) -> "Storage[User]":
    """Typed :class:`Storage` handle for :class:`User` rows.

    Mirrors :func:`get_llm_provider_storage`. Consumed by the admin
    users CRUD router and the change-password endpoint. ``User`` is
    already imported at module top.
    """
    return sp.get_storage(User)


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


def get_document_service(request: Request):
    """Build a request-scoped :class:`DocumentService` for the path-addressed
    document routes.

    The path-addressed routes do NOT go through ``make_crud_router`` (and so
    do not fire the Document CDC ``on_create`` / ``on_update`` indexing hook).
    We therefore wire the service with an explicit best-effort ``indexer``
    that re-indexes the body AFTER the atomic entity + content write commits
    (the service calls the hook only after its ``transaction()`` block exits),
    so a path-addressed PUT still indexes the document when search is on -
    behaviour-preserving relative to the CRUD path. The indexer is wired here
    rather than in the route so the dependency owns the registry lookups.
    """
    from primer.api.routers.knowledge import build_document_indexer

    sp = get_storage_provider(request)
    from primer.knowledge.document_service import DocumentService

    return DocumentService(sp, indexer=build_document_indexer(request))


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


def get_internal_collections_bootstrap_status_storage(
    sp: "StorageProvider" = Depends(get_storage_provider),
):
    from primer.model.internal import InternalCollectionsBootstrapStatus
    return sp.get_storage(InternalCollectionsBootstrapStatus)


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


def get_principal(request: Request) -> str | None:
    """Per-request end-user identity.

    Populated by :class:`primer.api.middleware.auth.AuthMiddleware`
    from the signed session cookie. ``None`` when the request is
    unauthenticated. Routers that need to enforce auth use
    :func:`require_auth` instead.
    """
    return getattr(request.state, "principal", None)


def require_auth(request: Request = None) -> User | None:  # type: ignore[assignment]
    """FastAPI dependency that enforces a valid session on HTTP routes.

    Returns the logged-in :class:`primer.model.user.User`. Raises
    HTTP 401 if the request didn't carry a valid signed cookie.
    Apply this to routers whose endpoints require authentication
    (i.e. everything under /v1/* except the auth router itself plus
    /v1/health, /metrics, and the env-gated /v1/_test/*).

    When a router serves BOTH HTTP and WebSocket endpoints, applying
    this dep at ``include_router(dependencies=...)`` time only catches
    the HTTP routes — FastAPI's dep resolver injects ``request=None``
    for WebSocket routes (since there is no HTTP Request in that
    scope), and we short-circuit to ``None`` for those. WS handlers
    are responsible for calling :func:`require_auth_ws` themselves.
    """
    if request is None:
        # WebSocket scope — no Request to verify. WS handlers MUST call
        # require_auth_ws() to enforce auth.
        return None
    user = getattr(request.state, "user", None)
    if not isinstance(user, User):
        raise HTTPException(
            status_code=401,
            detail={"error": "auth_required"},
        )
    return user


def require_user(request: Request = None) -> User | None:  # type: ignore[assignment]
    """FastAPI dependency: valid session AND a non-restricted role.

    Mirrors :func:`require_auth` — same WebSocket ``request is None``
    short-circuit and the same 401 ``auth_required`` when no valid
    session is present — then adds a role gate: ``role`` must be
    ``"user"`` or ``"admin"``. A ``restricted`` user is rejected with
    HTTP 403 ``{"error": "forbidden_role"}``. Apply to routers whose
    endpoints require an ordinary operator (agents, graphs, chats, …).
    """
    if request is None:
        # WebSocket scope — WS handlers call require_user_ws() instead.
        return None
    user = getattr(request.state, "user", None)
    if not isinstance(user, User):
        raise HTTPException(
            status_code=401,
            detail={"error": "auth_required"},
        )
    if user.role not in ("user", "admin"):
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden_role"},
        )
    return user


def require_admin(request: Request = None) -> User | None:  # type: ignore[assignment]
    """FastAPI dependency: valid session AND ``role == "admin"``.

    Mirrors :func:`require_auth` (WS short-circuit + 401 ``auth_required``)
    then requires an admin role, rejecting every other authenticated
    user with HTTP 403 ``{"error": "forbidden_role"}``. Apply to the
    provider / global-settings / admin routers.
    """
    if request is None:
        return None
    user = getattr(request.state, "user", None)
    if not isinstance(user, User):
        raise HTTPException(
            status_code=401,
            detail={"error": "auth_required"},
        )
    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden_role"},
        )
    return user


def require_auth_ws(websocket) -> User | None:
    """WebSocket-side counterpart to :func:`require_auth`.

    Returns the authenticated user, or ``None`` if the request was not
    authenticated. WS handlers should close the socket with a 4401 code
    when ``None`` is returned. Implemented as a plain helper rather than
    a FastAPI dep because FastAPI's WS dependency resolver does not
    inject :class:`Request` the same way it does for HTTP routes.
    """
    user = getattr(websocket.state, "user", None)
    return user if isinstance(user, User) else None


def require_user_ws(websocket) -> User | None:
    """WebSocket counterpart to :func:`require_user`.

    Returns the authenticated user when its ``role`` is ``"user"`` or
    ``"admin"``; returns ``None`` otherwise (unauthenticated OR a
    ``restricted`` role). WS handlers close the socket — 4401 when the
    request was never authenticated, or 4403 ``forbidden_role`` when a
    seated user's role is insufficient.
    """
    user = getattr(websocket.state, "user", None)
    if not isinstance(user, User):
        return None
    return user if user.role in ("user", "admin") else None


def require_role_ws(websocket, min_role: str) -> User | None:
    """Role-ranked WebSocket gate (``restricted`` < ``user`` < ``admin``).

    Returns the authenticated user when its role rank is at least
    ``min_role``'s rank; ``None`` otherwise (including unauthenticated).
    Fail-closed on unknown values: an unknown ``user.role`` ranks below
    ``restricted``; an unknown ``min_role`` ranks above ``admin``.
    """
    rank = {"restricted": 0, "user": 1, "admin": 2}
    user = getattr(websocket.state, "user", None)
    if not isinstance(user, User):
        return None
    have = rank.get(user.role, -1)
    need = rank.get(min_role, 99)
    return user if have >= need else None


def require_scope(scope: str):
    """FastAPI dep factory enforcing a bearer-token scope.

    Cookie sessions bypass this check (``request.state.api_token is
    None`` for cookie auth — they carry full user authority). Bearer
    tokens MUST include ``scope`` in their ``scopes`` list or the
    dep raises 403 with ``{"code": "scope_required", "scope": <scope>}``.

    Usage::

        @router.get(
            "/x",
            dependencies=[Depends(require_auth), require_scope("mcp")],
        )
        async def handler(...): ...
    """
    async def _dep(request: Request) -> None:
        api_token = getattr(request.state, "api_token", None)
        if api_token is None:
            return  # cookie session: bypass scope check
        if scope not in api_token.scopes:
            raise HTTPException(
                status_code=403,
                detail={"code": "scope_required", "scope": scope},
            )
    return Depends(_dep)


__all__ = [
    "get_approval_resolver",
    "get_agent_storage",
    "get_channel_dispatcher",
    "get_channel_inbox",
    "get_channel_registry",
    "get_chat_storage",
    "get_claim_engine",
    "get_collection_storage",
    "get_cross_encoder_provider_storage",
    "get_document_service",
    "get_document_storage",
    "get_embedding_provider_storage",
    "get_event_bus",
    "get_graph_storage",
    "get_ingest_failure_storage",
    "get_internal_collections_bootstrap_status_storage",
    "get_internal_collections_config_storage",
    "get_internal_collections_subsystem",
    "get_llm_provider_storage",
    "get_oidc_provider_storage",
    "get_user_storage",
    "get_principal",
    "get_provider_registry",
    "require_admin",
    "require_auth",
    "require_auth_ws",
    "require_role_ws",
    "require_user",
    "require_user_ws",
    "require_scope",
    "get_scheduler",
    "get_semantic_search_registry",
    "get_semantic_search_storage",
    "get_session_storage",
    "get_storage_provider",
    "get_toolset_storage",
    "get_user_identity_storage",
    "get_worker_pool",
    "get_workspace_provider_storage",
    "get_workspace_registry",
    "get_workspace_storage",
    "get_workspace_template_storage",
]
