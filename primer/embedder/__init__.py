"""Concrete embedder adapter implementations.

Each adapter subclasses :class:`matrix.int.Embedder` and implements the
embedding interface against one provider's SDK or local model.

Adapters land here one per file as the per-adapter sub-specs ship.
"""

from primer.embedder.gemini import GeminiEmbedder
from primer.embedder.huggingface import HuggingFaceEmbedder
from primer.embedder.openai import OpenAIEmbedder

__all__ = ["GeminiEmbedder", "HuggingFaceEmbedder", "OpenAIEmbedder"]
