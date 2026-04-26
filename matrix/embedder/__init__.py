"""Concrete embedder adapter implementations.

Each adapter subclasses :class:`matrix.int.Embedder` and implements the
embedding interface against one provider's SDK. The adapter takes an
:class:`matrix.model.provider.EmbeddingProvider` config object at
construction time and translates between the universal types in
:mod:`matrix.model.embedding` and the provider's wire format.

Adapters land here one per file as the per-adapter sub-specs ship.
"""

from matrix.embedder.openai import OpenAIEmbedder

__all__ = ["OpenAIEmbedder"]
