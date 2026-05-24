"""Lazy, invalidatable adapter registries for the FastAPI app.

* :class:`ProviderRegistry` — caches LLM/Embedder/CrossEncoder/Toolset
  adapter instances keyed by row id.
* :class:`SemanticSearchRegistry` — caches per-row VectorStoreProvider
  instances keyed by SemanticSearchProvider row id.
* :class:`WorkspaceRegistry` — caches workspace backend instances per
  configured provider id.
"""

from matrix.api.registries.provider_registry import ProviderRegistry
from matrix.api.registries.semantic_search_registry import (
    SemanticSearchRegistry,
)
from matrix.api.registries.workspace_registry import WorkspaceRegistry


__all__ = [
    "ProviderRegistry",
    "SemanticSearchRegistry",
    "WorkspaceRegistry",
]
