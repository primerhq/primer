"""Concrete LLM adapter implementations.

Each adapter subclasses :class:`matrix.int.LLM` and implements the
streaming chat interface against one provider's SDK. The adapter takes
an :class:`matrix.model.provider.LLMProvider` config object at
construction time (the configured-models list is the source of truth
for what the application is allowed to send) and translates between the
universal types in :mod:`matrix.model.chat` and the provider's wire
format.

Adapters land here one per file as the per-adapter sub-specs ship.
"""

from matrix.llm.gemini import GeminiLLM
from matrix.llm.openresponses import OpenResponsesLLM

__all__ = ["GeminiLLM", "OpenResponsesLLM"]
