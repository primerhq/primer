"""Lazy, invalidatable adapter registries for the FastAPI app.

* :class:`ProviderRegistry` — caches LLM/Embedder/CrossEncoder/Toolset
  adapter instances keyed by row id.
* :class:`VectorStoreRegistry` — caches the single active VectorStore.
"""

from matrix.api.registries.provider_registry import ProviderRegistry
from matrix.api.registries.vector_store_registry import (
    ACTIVE_VECTOR_STORE_CONFIG_ID,
    VectorStoreRegistry,
)


__all__ = [
    "ACTIVE_VECTOR_STORE_CONFIG_ID",
    "ProviderRegistry",
    "VectorStoreRegistry",
]
