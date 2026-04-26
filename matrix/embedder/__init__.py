"""Concrete embedder adapter implementations.

Each adapter subclasses :class:`matrix.int.Embedder` and implements the
embedding interface against one provider's SDK or local model.

Adapters land here one per file as the per-adapter sub-specs ship.
"""

from matrix.embedder.gemini import GeminiEmbedder
from matrix.embedder.huggingface import HuggingFaceEmbedder
from matrix.embedder.openai import OpenAIEmbedder

__all__ = ["GeminiEmbedder", "HuggingFaceEmbedder", "OpenAIEmbedder"]
