"""Provider entity routers: LLM / Embedding / CrossEncoder / Toolset.

Each entity follows the standard CRUD + Find shape from
:mod:`matrix.api.routers._crud`, plus entity-specific operations:

* LLMProvider:           ``GET /v1/llm_providers/{id}/models``
                         ``POST /v1/llm_providers/{id}/invalidate``
* EmbeddingProvider:     ``GET /v1/embedding_providers/{id}/models``
                         ``POST /v1/embedding_providers/{id}/invalidate``
* CrossEncoderProvider:  ``GET /v1/cross_encoder_providers/{id}/models``
                         ``POST /v1/cross_encoder_providers/{id}/invalidate``
* Toolset:               ``GET  /v1/toolsets/{id}/tools``
                         ``POST /v1/toolsets/{id}/invalidate``

PUT and DELETE on every entity cascade-invalidate the matching cached
adapter in the per-request ProviderRegistry via the CRUD callbacks.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Path, Request

from matrix.api.deps import (
    PRINCIPAL_HEADER,
    get_cross_encoder_provider_storage,
    get_embedding_provider_storage,
    get_llm_provider_storage,
    get_provider_registry,
    get_toolset_storage,
)
from matrix.api.errors import common_responses
from matrix.api.registries import ProviderRegistry
from matrix.api.routers._crud import make_crud_router
from matrix.model.provider import (
    CrossEncoderProvider,
    EmbeddingProvider,
    LLMProvider,
    Toolset,
)


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


# ---- Toolset router --------------------------------------------------------

toolset_router = make_crud_router(
    model_cls=Toolset,
    storage_dep=get_toolset_storage,
    plural="toolsets",
    tag="toolsets",
    on_update=_invalidate_toolset,
    on_delete=_invalidate_toolset,
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
    except Exception as exc:
        # Re-raise the documented matrix error types so the registry
        # mapper produces the correct envelope (NotFoundError → 404,
        # AuthRequiredError → 401, etc.).
        from matrix.model.except_ import MatrixError
        if isinstance(exc, MatrixError):
            raise
        # MCP transport failures (handshake refused, connection closed,
        # subprocess crash) come back as third-party exception types
        # like mcp.shared.exceptions.McpError. Map them to 502
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
