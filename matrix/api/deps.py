"""FastAPI ``Depends`` helpers.

Three layers:

1. Singleton resolvers that read pre-built dependencies from
   ``app.state``.
2. Per-model ``Storage[T]`` resolvers that use the
   :class:`StorageProvider` to fetch the right typed handle.
3. Principal passthrough that pulls the optional
   ``X-Matrix-Principal`` request header.

The lifespan handler (or test factory) MUST stash three attributes on
``app.state`` before the first request: ``storage_provider``,
``provider_registry``, ``vector_store_registry``. Each resolver
defends against missing state by raising ``ConfigError``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, Header, Request

from matrix.api.registries import ProviderRegistry, VectorStoreRegistry
from matrix.model.agent import Agent
from matrix.model.collection import Collection, Document
from matrix.model.except_ import ConfigError
from matrix.model.graph import Graph
from matrix.model.internal import IngestFailure, InternalCollectionsConfig
from matrix.model.provider import (
    CrossEncoderProvider,
    EmbeddingProvider,
    LLMProvider,
    Toolset,
)


if TYPE_CHECKING:
    from matrix.int.storage import Storage
    from matrix.int.storage_provider import StorageProvider


PRINCIPAL_HEADER = "X-Matrix-Principal"


def _assert_app_state_initialized(request: Request) -> None:
    state = request.app.state
    missing = [
        name
        for name in ("storage_provider", "provider_registry", "vector_store_registry")
        if not hasattr(state, name) or getattr(state, name) is None
    ]
    if missing:
        raise ConfigError(
            f"API state not initialised; missing attributes on app.state: "
            f"{', '.join(missing)}. The lifespan handler (or "
            "create_test_app) must set storage_provider, "
            "provider_registry, and vector_store_registry before any "
            "request is served."
        )


def get_storage_provider(request: Request) -> "StorageProvider":
    _assert_app_state_initialized(request)
    return request.app.state.storage_provider


def get_provider_registry(request: Request) -> ProviderRegistry:
    _assert_app_state_initialized(request)
    return request.app.state.provider_registry


def get_vector_store_registry(request: Request) -> VectorStoreRegistry:
    _assert_app_state_initialized(request)
    return request.app.state.vector_store_registry


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


def get_principal(
    x_matrix_principal: str | None = Header(default=None, alias=PRINCIPAL_HEADER),
) -> str | None:
    """Per-request end-user identity. ``None`` if header absent."""
    return x_matrix_principal


__all__ = [
    "PRINCIPAL_HEADER",
    "get_agent_storage",
    "get_collection_storage",
    "get_cross_encoder_provider_storage",
    "get_document_storage",
    "get_embedding_provider_storage",
    "get_graph_storage",
    "get_ingest_failure_storage",
    "get_internal_collections_config_storage",
    "get_internal_collections_subsystem",
    "get_llm_provider_storage",
    "get_principal",
    "get_provider_registry",
    "get_storage_provider",
    "get_toolset_storage",
    "get_vector_store_registry",
]
