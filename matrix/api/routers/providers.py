"""Provider entity routers: LLM / Embedding / CrossEncoder / Toolset.

Each entity follows the standard CRUD + Find shape from
:mod:`matrix.api.routers._crud`, plus entity-specific operations:

* LLMProvider:           ``GET /v1/llm_providers/{id}/models``
                         ``POST /v1/llm_providers/{id}/invalidate``
                         ``POST /v1/llm_providers/_discover_models``
* EmbeddingProvider:     ``GET /v1/embedding_providers/{id}/models``
                         ``POST /v1/embedding_providers/{id}/invalidate``
                         ``POST /v1/embedding_providers/_discover_models``
* CrossEncoderProvider:  ``GET /v1/cross_encoder_providers/{id}/models``
                         ``POST /v1/cross_encoder_providers/{id}/invalidate``
* Toolset:               ``GET  /v1/toolsets/{id}/tools``
                         ``POST /v1/toolsets/{id}/invalidate``

PUT and DELETE on every entity cascade-invalidate the matching cached
adapter in the per-request ProviderRegistry via the CRUD callbacks.

The ``_discover_models`` endpoints accept a draft provider config
(provider type + config block) and live-probe the upstream API to
return the list of available models — used by the console's "Fetch
Models" button. They build a transient adapter, call its
``list_models()``, then dispose. They do NOT persist anything.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, Header, Path, Request
from pydantic import BaseModel, Field, ValidationError

from matrix.api.deps import (
    PRINCIPAL_HEADER,
    get_cross_encoder_provider_storage,
    get_embedding_provider_storage,
    get_llm_provider_storage,
    get_provider_registry,
    get_toolset_storage,
)
from matrix.api.errors import common_responses
from matrix.model.except_ import BadRequestError, ConflictError
from matrix.api.registries import ProviderRegistry
from matrix.api.routers._crud import make_crud_router
from matrix.model.provider import (
    CrossEncoderProvider,
    EmbeddingProvider,
    LLMProvider,
    Toolset,
)
from matrix.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from matrix.model.tool_approval import ToolApprovalPolicy


# ---- Discovery body shapes -------------------------------------------------


class _DiscoverModelsBody(BaseModel):
    """Body for ``POST /v1/<entity>/_discover_models``.

    A draft provider config — same shape as a real provider entry but
    without ``id``, ``models``, or ``limits`` (the endpoint synthesizes
    those to satisfy the model validator before constructing the
    transient adapter).
    """

    provider: str = Field(..., description="Provider type discriminator.")
    config: dict[str, Any] = Field(
        ..., description="Provider-specific connection config.",
    )


def _build_stub_provider(
    model_cls: type,
    *,
    provider: str,
    config: dict[str, Any],
    models: list[dict[str, Any]],
) -> Any:
    """Construct a transient provider row for discovery probes.

    Validates the draft via the canonical Pydantic model so config-
    shape errors surface as a 400 with the field path, instead of an
    obscure 500 from inside the adapter constructor. The id and limits
    are synthesized — neither matters for ``list_models()``.
    """
    try:
        return model_cls.model_validate({
            "id": f"_probe_{uuid4().hex[:8]}",
            "provider": provider,
            "config": config,
            "models": models,
            "limits": {"max_concurrency": 1},
        })
    except ValidationError as e:
        raise BadRequestError(
            "Draft provider failed validation: " + str(e),
        ) from e


# ---- cascade-invalidation hooks --------------------------------------------


def _make_invalidator(method_name: str):
    """Build a CRUD-router hook that invalidates the named cache slot
    on the request's :class:`ProviderRegistry`."""
    async def _hook(entity_id: str, request: Request) -> None:
        registry: ProviderRegistry = request.app.state.provider_registry
        await getattr(registry, method_name)(entity_id)
    _hook.__name__ = f"_invalidate_{method_name.removeprefix('invalidate_')}"
    return _hook


_invalidate_llm = _make_invalidator("invalidate_llm")
_invalidate_embedder = _make_invalidator("invalidate_embedder")
_invalidate_cross_encoder = _make_invalidator("invalidate_cross_encoder")
_invalidate_toolset = _make_invalidator("invalidate_toolset")


# ---- LLMProvider router ----------------------------------------------------

llm_provider_router = make_crud_router(
    model_cls=LLMProvider,
    storage_dep=get_llm_provider_storage,
    plural="llm_providers",
    tag="llm-providers",
    on_update=_invalidate_llm,
    on_delete=_invalidate_llm,
)


@llm_provider_router.post(
    "/llm_providers/{provider_id}/invalidate",
    status_code=204,
    summary="Invalidate cached LLM adapter",
    responses=common_responses(500),
)
async def invalidate_llm_provider(
    provider_id: str = Path(..., description="LLMProvider id"),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> None:
    await registry.invalidate_llm(provider_id)


@llm_provider_router.get(
    "/llm_providers/{provider_id}/models",
    summary="Fetch live model list from the LLM provider",
    responses=common_responses(404, 500, 502, 504),
)
async def get_llm_provider_models(
    provider_id: str = Path(..., description="LLMProvider id"),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> dict:
    adapter = await registry.get_llm(provider_id)
    models = await adapter.list_models()
    return {"models": list(models)}


@llm_provider_router.post(
    "/llm_providers/_discover_models",
    summary="Probe a draft LLM provider for its model list",
    responses=common_responses(400, 422, 502),
)
async def discover_llm_models(
    body: _DiscoverModelsBody = Body(...),
) -> dict:
    """Live-probe the upstream provider for its available models.

    Used by the console's "Fetch Models" button before any provider
    has been persisted. Returns ``{"models": [{name, context_length?}, ...]}``.

    Note: the adapters' ``list_models()`` method returns the stored
    row's static model list (anomaly T0025), not a live probe. This
    endpoint deliberately bypasses the adapter for discovery and calls
    the provider's native list endpoint directly.

    Only ``ollama`` and ``openresponses`` expose a useful list-models
    API. For ``anthropic`` and ``gemini`` the frontend should fall back
    to a curated suggested-model list — those providers return 400 here.
    """
    # Validate the draft via the canonical model so config shape errors
    # surface cleanly. We never persist or run anything from the stub.
    _build_stub_provider(
        LLMProvider,
        provider=body.provider,
        config=body.config,
        models=[{"name": "_probe", "context_length": 1}],
    )
    if body.provider == "ollama":
        return await _probe_ollama_models(body.config)
    if body.provider == "openresponses":
        return await _probe_openai_compatible_models(body.config)
    raise BadRequestError(
        f"live model discovery is not supported for provider "
        f"{body.provider!r}; populate the models list manually or "
        f"use the UI's 'Suggest models' fallback.",
    )


# ---- EmbeddingProvider router ----------------------------------------------

embedding_provider_router = make_crud_router(
    model_cls=EmbeddingProvider,
    storage_dep=get_embedding_provider_storage,
    plural="embedding_providers",
    tag="embedding-providers",
    on_update=_invalidate_embedder,
    on_delete=_invalidate_embedder,
)


@embedding_provider_router.post(
    "/embedding_providers/{provider_id}/invalidate",
    status_code=204,
    summary="Invalidate cached embedder adapter",
    responses=common_responses(500),
)
async def invalidate_embedding_provider(
    provider_id: str = Path(..., description="EmbeddingProvider id"),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> None:
    await registry.invalidate_embedder(provider_id)


@embedding_provider_router.get(
    "/embedding_providers/{provider_id}/models",
    summary="Fetch live model list from the embedding provider",
    responses=common_responses(404, 500, 502, 504),
)
async def get_embedding_provider_models(
    provider_id: str = Path(..., description="EmbeddingProvider id"),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> dict:
    adapter = await registry.get_embedder(provider_id)
    models = await adapter.list_models()
    return {"models": list(models)}


@embedding_provider_router.post(
    "/embedding_providers/_discover_models",
    summary="Probe a draft embedding provider for its model list",
    responses=common_responses(400, 422, 502),
)
async def discover_embedding_models(
    body: _DiscoverModelsBody = Body(...),
) -> dict:
    """Mirror of ``discover_llm_models`` for embedding providers.

    Only ``openai`` (OpenAI-compatible HTTP) is live-discoverable.
    ``huggingface`` (local sentence-transformers) and ``gemini`` have
    no usable list endpoint — the frontend falls back to curated
    suggestions.
    """
    _build_stub_provider(
        EmbeddingProvider,
        provider=body.provider,
        config=body.config,
        models=[{"name": "_probe"}],
    )
    if body.provider == "openai":
        return await _probe_openai_compatible_models(body.config)
    raise BadRequestError(
        f"live model discovery is not supported for embedding "
        f"provider {body.provider!r}; populate the models list "
        f"manually or use the UI's 'Suggest models' fallback.",
    )


# ---- Discovery probes ------------------------------------------------------


async def _probe_ollama_models(config: dict[str, Any]) -> dict:
    """List models locally available on an Ollama server."""
    import ollama

    headers: dict[str, str] = {}
    api_key = config.get("api_key")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client = ollama.AsyncClient(host=config["url"], headers=headers or None)
    try:
        resp = await client.list()
    except Exception as exc:  # pragma: no cover — network paths
        raise BadRequestError(
            f"ollama probe failed: {type(exc).__name__}: {exc}",
        ) from exc
    finally:
        # ollama.AsyncClient does not expose aclose() in all versions;
        # rely on httpx's GC for cleanup.
        pass
    # ollama.list() returns a ListResponse with .models[].model (the name).
    # context_length is not exposed on the list endpoint; would need a
    # per-model `client.show(name)` call. Skip — operators edit it in
    # the form.
    models = getattr(resp, "models", None) or resp.get("models", [])
    out: list[dict[str, Any]] = []
    for m in models:
        name = getattr(m, "model", None) or (m.get("model") if isinstance(m, dict) else None)
        if name:
            out.append({"name": name})
    return {"models": out}


async def _probe_openai_compatible_models(config: dict[str, Any]) -> dict:
    """List models from an OpenAI-compatible /v1/models endpoint."""
    import httpx

    url = str(config["url"]).rstrip("/")
    api_key = config.get("api_key") or ""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{url}/models", headers=headers)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # pragma: no cover — network paths
        raise BadRequestError(
            f"openai-compatible probe failed: {type(exc).__name__}: {exc}",
        ) from exc
    items = data.get("data") or []
    # OpenAI returns {data: [{id, ...}, ...]}. context_length isn't in
    # the list response (it's model-specific; the chat-completions API
    # returns it on a per-request error path). The UI seeds a default
    # context_length and the operator can edit it.
    return {"models": [{"name": m["id"]} for m in items if "id" in m]}


# ---- CrossEncoderProvider router -------------------------------------------

cross_encoder_provider_router = make_crud_router(
    model_cls=CrossEncoderProvider,
    storage_dep=get_cross_encoder_provider_storage,
    plural="cross_encoder_providers",
    tag="cross-encoder-providers",
    on_update=_invalidate_cross_encoder,
    on_delete=_invalidate_cross_encoder,
)


@cross_encoder_provider_router.post(
    "/cross_encoder_providers/{provider_id}/invalidate",
    status_code=204,
    summary="Invalidate cached cross-encoder adapter",
    responses=common_responses(500),
)
async def invalidate_cross_encoder_provider(
    provider_id: str = Path(..., description="CrossEncoderProvider id"),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> None:
    await registry.invalidate_cross_encoder(provider_id)


@cross_encoder_provider_router.get(
    "/cross_encoder_providers/{provider_id}/models",
    summary="Fetch live model list from the cross-encoder provider",
    responses=common_responses(404, 500, 502, 504),
)
async def get_cross_encoder_provider_models(
    provider_id: str = Path(..., description="CrossEncoderProvider id"),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> dict:
    adapter = await registry.get_cross_encoder(provider_id)
    models = await adapter.list_models()
    return {"models": list(models)}


# ---- Toolset delete: cascade-block + invalidate ----------------------------


async def _toolset_on_delete(entity_id: str, request: Request) -> None:
    """Block delete when a ToolApprovalPolicy references this toolset; then invalidate."""
    storage_provider = request.app.state.storage_provider
    policy_storage = storage_provider.get_storage(ToolApprovalPolicy)
    page = await policy_storage.find(
        Predicate(
            left=FieldRef(name="toolset_id"),
            op=Op.EQ,
            right=Value(value=entity_id),
        ),
        OffsetPage(offset=0, length=1),
    )
    if page.items:
        raise ConflictError(
            f"Toolset {entity_id!r} cannot be deleted while "
            f"ToolApprovalPolicy {page.items[0].id!r} still "
            "references it"
        )
    await _invalidate_toolset(entity_id, request)


# ---- Toolset router --------------------------------------------------------

toolset_router = make_crud_router(
    model_cls=Toolset,
    storage_dep=get_toolset_storage,
    plural="toolsets",
    tag="toolsets",
    on_update=_invalidate_toolset,
    on_delete=_toolset_on_delete,
)


@toolset_router.post(
    "/toolsets/{toolset_id}/invalidate",
    status_code=204,
    summary="Invalidate cached toolset provider",
    responses=common_responses(500),
)
async def invalidate_toolset_provider(
    toolset_id: str = Path(..., description="Toolset id"),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> None:
    await registry.invalidate_toolset(toolset_id)


@toolset_router.get(
    "/toolsets/{toolset_id}/tools",
    summary="List tools currently exposed by a toolset",
    responses=common_responses(401, 404, 500, 502, 504),
)
async def list_toolset_tools(
    toolset_id: str = Path(..., description="Toolset id"),
    principal: str | None = Header(default=None, alias=PRINCIPAL_HEADER),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> dict:
    """Enumerate the toolset's tools from the live provider.

    OAuth-protected MCP toolsets raise ``AuthRequiredError``, which the
    error mapper serialises as 401 + ``extensions.auth_url`` so the
    caller can prompt the user to consent.
    """
    from matrix.model.except_ import ProviderError

    provider = await registry.get_toolset(toolset_id)
    tools = []
    try:
        async for tool in provider.list_tools(principal=principal):
            tools.append(tool.model_dump(mode="json"))
    except BaseExceptionGroup as group:
        # HTTP/SSE MCP transports run inside anyio task groups, which
        # wrap any sub-exception in BaseExceptionGroup. Unwrap to find
        # the most informative inner exception, then map to the same
        # ProviderError envelope as the plain-Exception path.
        from matrix.model.except_ import MatrixError
        # Surface a documented MatrixError if any sub-exception is one
        for sub in group.exceptions:
            if isinstance(sub, MatrixError):
                raise sub from group
        # Otherwise, take the first sub-exception's type+message
        first = group.exceptions[0] if group.exceptions else group
        raise ProviderError(
            f"toolset {toolset_id!r} provider failed to enumerate tools: "
            f"{type(first).__name__}: {first}"
        ) from group
    except Exception as exc:
        # Re-raise the documented matrix error types so the registry
        # mapper produces the correct envelope (NotFoundError → 404,
        # AuthRequiredError → 401, etc.).
        from matrix.model.except_ import MatrixError
        if isinstance(exc, MatrixError):
            raise
        # MCP stdio transport failures (handshake refused, connection
        # closed, subprocess crash) come back as third-party exception
        # types like mcp.shared.exceptions.McpError. Map to 502
        # provider-error rather than letting the 500 leak.
        raise ProviderError(
            f"toolset {toolset_id!r} provider failed to enumerate tools: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return {"tools": tools}


__all__ = [
    "cross_encoder_provider_router",
    "embedding_provider_router",
    "llm_provider_router",
    "toolset_router",
]
