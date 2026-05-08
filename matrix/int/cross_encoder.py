"""Abstract base class for cross-encoder rerankers.

Sibling of :class:`matrix.int.LLM`, :class:`matrix.int.Embedder`,
:class:`matrix.int.VectorStore`, :class:`matrix.int.ToolsetProvider`,
and :class:`matrix.int.Storage`. A :class:`CrossEncoder` instance is
bound to one provider (HuggingFace local, Cohere, Jina, …) at
construction time and may serve multiple cross-encoder models — the
``model`` parameter on :meth:`score` selects which one to use for a
given call.

Cross-encoders score a ``(query, document)`` pair jointly: unlike
dual-encoders / embedders they do NOT produce a reusable vector
representation, so this interface returns scalar relevance scores
rather than vectors. Higher scores mean stronger matches.

Score scale is provider-specific (raw logits for sentence-transformers
``cross-encoder/*`` and ``BAAI/bge-reranker-*``; pre-sigmoided
``[0, 1]`` for hosted Cohere / Jina rerankers). Callers MUST treat
scores as backend-relative — use them only to sort or threshold within
one call, never to compare across providers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable


class CrossEncoder(ABC):
    """Provider-agnostic ``(query, document) -> relevance score`` interface."""

    @abstractmethod
    async def list_models(self) -> Iterable[str]:
        """Return the names of cross-encoder models served by this provider.

        Returns an iterable rather than a list so adapters that
        paginate the underlying SDK call can yield results lazily.
        """

    @abstractmethod
    async def score(
        self,
        *,
        model: str,
        query: str,
        documents: list[str],
        batch_size: int = 32,
    ) -> list[float]:
        """Score every document against the query; return one score per document.

        Parameters
        ----------
        model
            Provider-side cross-encoder model name. Must be one of
            the names returned by :meth:`list_models`; adapters
            should validate before dispatch and raise
            :class:`matrix.model.except_.ConfigError` on a miss.
        query
            The retrieval query as plain text.
        documents
            The candidate documents as plain text, in the order the
            caller wants the scores returned.
        batch_size
            How many ``(query, doc)`` pairs the underlying predictor
            scores per micro-batch. Defaults to 32 (the
            sentence-transformers default and a safe CPU choice).
            Adapters that don't batch internally MAY ignore this.

        Returns
        -------
        list[float]
            One relevance score per input document, **in input
            order**. Empty input ⇒ empty list (no model call).

        Raises
        ------
        matrix.model.except_.ConfigError
            ``model`` is not one of this provider's permitted models,
            or the underlying backend library is misconfigured.
        matrix.model.except_.ProviderError
            The remote provider (Cohere / Jina) returned an error
            response or an unexpected payload.
        matrix.model.except_.NetworkError
            Network failure communicating with a remote provider.
        """


__all__ = ["CrossEncoder"]
