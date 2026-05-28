"""Concrete :class:`matrix.int.CrossEncoder` adapters.

Sibling of :mod:`matrix.embedder` and :mod:`matrix.llm`. Each adapter
binds the ABC to one provider backend.

The default adapter is :class:`HuggingFaceCrossEncoder`, which wraps
:class:`sentence_transformers.CrossEncoder` for local inference.
Future managed-API adapters (Cohere, Jina) drop in alongside it
without touching the ABC or the
:class:`matrix.search.CollectionSearcher` orchestrator.
"""

from primer.cross_encoder.huggingface import HuggingFaceCrossEncoder


__all__ = ["HuggingFaceCrossEncoder"]
