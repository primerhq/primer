"""Abstract base class for embedding providers.

Implementations bind to a configured provider (URL, credentials, rate
limits, etc.) at construction time and may serve multiple embedding
models — model selection happens per call.

The signature was derived from the cross-SDK comparison documented in
``research/embedding_interface.md`` and ``research/abc_interface.md``.
The ``inputs`` and ``config`` parameters reuse types from
:mod:`matrix.model.embedding`; ``output_dimensions`` is the universal
Matryoshka-truncation knob (OpenAI ``dimensions``, Google
``output_dimensionality``, Ollama ``dimensions``).

Adapters that don't support a given input modality (e.g. OpenAI
embeddings reject any non-text :class:`EmbeddingPart`) MUST raise a
typed error rather than silently dropping the input.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable

from matrix.model.embedding import (
    EmbeddingPart,
    EmbedResponse,
    ExtendedEmbeddingConfig,
)


class Embedder(ABC):
    """Provider-agnostic embedding interface.

    Subclasses are bound to one configured provider but may dispatch to
    multiple embedding models on it. The ``model`` parameter on
    :meth:`embed` selects which one to use for a given call.
    """

    @abstractmethod
    async def list_models(self) -> Iterable[str]:
        """Return the names of embedding models served by this provider.

        Returns an iterable rather than a list so adapters that paginate
        the underlying SDK call can yield results lazily.
        """

    @abstractmethod
    async def embed(
        self,
        *,
        model: str,
        inputs: list[EmbeddingPart],
        output_dimensions: int | None = None,
        config: ExtendedEmbeddingConfig | None = None,
    ) -> EmbedResponse:
        """Produce one embedding vector per input, in input order.

        Parameters
        ----------
        model
            Provider-side model identifier. Must be one of the names
            returned by :meth:`list_models`; adapters should validate
            this before dispatch.
        inputs
            Ordered list of :class:`EmbeddingPart`s to embed. Universal
            members (:class:`TextPart`, :class:`ImagePart`) are accepted
            by adapters whose providers support them; adapters whose
            providers do not (e.g. OpenAI rejects images) raise a typed
            error rather than silently dropping. Extended parts (audio,
            video, document, pre-tokenised) reach the adapter through
            :class:`ExtendedEmbeddingPart`.
        output_dimensions
            Truncate vectors to this dimensionality (Matryoshka).
            Supported by OpenAI ``text-embedding-3`` family and Google
            ``gemini-embedding-2`` family; ignored elsewhere.
        config
            Provider-specific request-level configuration knobs:
            ``task_type``, ``title``, ``auto_truncate``, ``document_ocr``,
            ``audio_track_extraction``, ``user``, plus a ``raw`` escape
            hatch. See :class:`ExtendedEmbeddingConfig` for the field
            inventory and per-provider applicability.

        Returns
        -------
        EmbedResponse
            One :class:`Embedding` per input in input order, plus
            optional :class:`EmbeddingUsage` request-level telemetry.
            See :mod:`matrix.model.embedding` for the full output shape.
        """

    async def aclose(self) -> None:
        """Release backend resources held by this adapter. Default no-op."""
        return
