"""Lazy, invalidatable adapter registries for the FastAPI app.

* :class:`ProviderRegistry` — caches LLM/Embedder/CrossEncoder/Toolset
  adapter instances keyed by row id.
* :class:`SemanticSearchRegistry` — caches per-row VectorStoreProvider
  instances keyed by SemanticSearchProvider row id.
* :class:`WorkspaceRegistry` — caches workspace backend instances per
  configured provider id.
"""

from primer.api.registries.channel_registry import ChannelRegistry
from primer.api.registries.provider_registry import ProviderRegistry
from primer.api.registries.semantic_search_registry import (
    SemanticSearchRegistry,
)
from primer.api.registries.workspace_registry import WorkspaceRegistry


__all__ = [
    "ChannelRegistry",
    "ProviderRegistry",
    "SemanticSearchRegistry",
    "WorkspaceRegistry",
]
