"""Concrete :class:`primer.int.CrossEncoder` adapters.

Sibling of :mod:`primer.embedder` and :mod:`primer.llm`. Each adapter
binds the ABC to one provider backend.

The default adapter is :class:`HuggingFaceCrossEncoder`, which wraps
:class:`sentence_transformers.CrossEncoder` for local inference and
requires the optional ``huggingface`` extra (``sentence-transformers`` ->
``torch``). It is imported lazily via :pep:`562` ``__getattr__`` so that
importing this package never pulls the heavy ML stack. Future managed-API
adapters (Cohere, Jina) drop in alongside it without touching the ABC or
the :class:`primer.search.CollectionSearcher` orchestrator.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primer.cross_encoder.huggingface import HuggingFaceCrossEncoder


__all__ = ["HuggingFaceCrossEncoder"]


def __getattr__(name: str):
    if name == "HuggingFaceCrossEncoder":
        try:
            from primer.cross_encoder.huggingface import HuggingFaceCrossEncoder
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise ModuleNotFoundError(
                "HuggingFaceCrossEncoder requires the optional 'huggingface' "
                "extra. Install it with: pip install 'primer-ai[huggingface]' "
                "(or 'primer-ai[full]' for everything)."
            ) from exc
        return HuggingFaceCrossEncoder
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
