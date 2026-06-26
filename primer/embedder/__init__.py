"""Concrete embedder adapter implementations.

Each adapter subclasses :class:`primer.int.Embedder` and implements the
embedding interface against one provider's SDK or local model.

``HuggingFaceEmbedder`` requires the optional ``huggingface`` extra
(``sentence-transformers`` -> ``torch``). It is imported lazily via
:pep:`562` ``__getattr__`` so that importing this package, or selecting an
API-based embedder (Gemini / OpenAI), never pulls the heavy ML stack.
"""

from typing import TYPE_CHECKING

from primer.embedder.gemini import GeminiEmbedder
from primer.embedder.openai import OpenAIEmbedder

if TYPE_CHECKING:
    from primer.embedder.huggingface import HuggingFaceEmbedder

__all__ = ["GeminiEmbedder", "HuggingFaceEmbedder", "OpenAIEmbedder"]


def __getattr__(name: str):
    if name == "HuggingFaceEmbedder":
        try:
            from primer.embedder.huggingface import HuggingFaceEmbedder
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise ModuleNotFoundError(
                "HuggingFaceEmbedder requires the optional 'huggingface' "
                "extra. Install it with: pip install 'primer-ai[huggingface]' "
                "(or 'primer-ai[full]' for everything)."
            ) from exc
        return HuggingFaceEmbedder
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
