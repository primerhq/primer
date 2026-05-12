"""Lazy, invalidatable adapter registries for the FastAPI app.

* :class:`ProviderRegistry` — caches LLM/Embedder/CrossEncoder/Toolset
  adapter instances keyed by row id.
* :class:`VectorStoreRegistry` — caches the single active VectorStore.
* :class:`WorkspaceRegistry` — caches workspace backend instances per
  configured provider id.
"""

from matrix.api.registries.provider_registry import ProviderRegistry
from matrix.api.registries.vector_store_registry import VectorStoreRegistry
from matrix.api.registries.workspace_registry import WorkspaceRegistry


__all__ = [
    "ProviderRegistry",
    "VectorStoreRegistry",
    "WorkspaceRegistry",
]
