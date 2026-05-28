"""Provider-agnostic embedding input/output types.

Embedding-side counterpart to :mod:`matrix.model.chat`. Built from
comparative SDK research in ``research/embedding_interface.md`` covering
OpenAI embeddings, Google GenAI ``embed_content``, and HuggingFace
``transformers``.

The interface follows the same layered design as ``chat.py``:

* **Universal types** — :class:`TextPart` and :class:`ImagePart` (re-exported
  from :mod:`matrix.model.chat`) — are the primary surface. Every adapter
  must accept them, even if it has to reject some at runtime (e.g. the
  OpenAI adapter rejects images because OpenAI embeddings are text-only).
* **Extended types** — audio, video, document/PDF, and pre-tokenised input
  — live behind a single :class:`ExtendedEmbeddingPart` wrapper reachable
  via an ``extended`` property. The Embedding output likewise carries an
  optional :class:`ExtendedEmbeddingMetadata` envelope for provider-specific
  per-input metadata (token counts, aligned secondary vectors, per-token
  vectors).

Two top-level discriminated unions are exported:

* :data:`EmbeddingPart` — one element of the ``inputs`` list passed to an
  embedding adapter. Members: :class:`TextPart`, :class:`ImagePart`, and
  :class:`ExtendedEmbeddingPart`.
* :data:`ExtendedEmbeddingInput` — the inner union of part types only
  reachable through the wrapper.

Note on universal/extended split for embeddings vs chat:

* :class:`DocumentPart` is *universal* in chat (every chat backend accepts
  PDFs) but *extended* here (only Vertex ``gemini-embedding-2-*`` with
  ``document_ocr=True`` ingests documents directly; everywhere else the
  caller must pre-extract text).
* :class:`ImagePart` stays universal here (Google ``gemini-embedding-2-*``
  and Transformers CLIP/SigLIP/BLIP handle it; OpenAI must reject).

Adapters MUST prefer the universal type whenever a clean mapping exists;
use the extended wrapper only when no subset equivalent captures the
signal without information loss.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, PositiveInt

from primer.model.chat import (
    AudioPart,
    DocumentPart,
    ImagePart,
    TextPart,
    VideoPart,
)


__all__ = [
    # Re-exported universal parts from chat
    "TextPart",
    "ImagePart",
    # Extended part types
    "AudioPart",
    "VideoPart",
    "DocumentPart",
    "TokensPart",
    "ExtendedEmbeddingInput",
    "ExtendedEmbeddingPart",
    # Top-level input union
    "EmbeddingPart",
    # Request-level config
    "TaskType",
    "ExtendedEmbeddingConfig",
    # Output payloads
    "PerInputUsage",
    "AlignedModalityVector",
    "PerTokenVectors",
    "ExtendedEmbeddingMetadata",
    "Embedding",
    "EmbeddingUsage",
    "EmbedResponse",
    "OutputDimensions",
]


# ===========================================================================
# Input parts
# ===========================================================================


# ---- Extended input parts (wrapped via ExtendedEmbeddingPart) ---------------


class TokensPart(BaseModel):
    """Pre-tokenised input. Reachable only through :class:`ExtendedEmbeddingPart`.

    Currently supported only by the OpenAI embeddings adapter
    (``input: Iterable[int] | Iterable[Iterable[int]]``). Useful when the
    caller has already tokenised the text with the provider's tokenizer
    and wants to skip server-side re-tokenisation. All other adapters
    must reject this part.

    The optional ``tokenizer`` field lets the caller record which
    tokenizer produced the IDs so the adapter can sanity-check (e.g.
    OpenAI uses ``cl100k_base`` for the v3 embedding family).
    """

    type: Literal["tokens"] = Field(
        default="tokens",
        description="Discriminator tag identifying this extended part as pre-tokenised input.",
    )
    tokens: list[int] = Field(
        ...,
        min_length=1,
        description="Sequence of token IDs from the provider's tokenizer.",
    )
    tokenizer: str | None = Field(
        default=None,
        description="Name of the tokenizer that produced the IDs (e.g. 'cl100k_base'). Adapter may sanity-check.",
    )


ExtendedEmbeddingInput = Annotated[
    AudioPart | VideoPart | DocumentPart | TokensPart,
    Field(discriminator="type"),
]
"""Discriminated union of every extended embedding-input part.

Always reached via :attr:`ExtendedEmbeddingPart.extended`. New extended
part types are added here; consumers gain access to them automatically
by matching on :class:`ExtendedEmbeddingPart`.
"""


class ExtendedEmbeddingPart(BaseModel):
    """Wrapper carrying a non-universal embedding input part.

    The unified :data:`EmbeddingPart` union exposes only the modalities
    every surveyed embedding backend can accept as first-class members
    (text, image). Modalities and shapes that some providers cannot
    ingest — audio, video, document/PDF, pre-tokenised input — are
    wrapped in this single envelope so universal-only consumers can
    identify and skip them with one pattern-match arm.

    Construct as ``ExtendedEmbeddingPart(extended=AudioPart(...))``;
    access the payload via ``part.extended``.
    """

    type: Literal["extended"] = Field(
        default="extended",
        description="Discriminator tag identifying this part as an extended-content wrapper.",
    )
    extended: ExtendedEmbeddingInput = Field(
        ...,
        description="The wrapped extended embedding-input part (audio, video, document, tokens).",
    )


# ---- Top-level EmbeddingPart union ------------------------------------------


EmbeddingPart = Annotated[
    TextPart | ImagePart | ExtendedEmbeddingPart,
    Field(discriminator="type"),
]
"""One element of the ``inputs`` list passed to an embedding adapter.

Universal members (:class:`TextPart`, :class:`ImagePart`) are the primary
surface — adapters should always prefer one of these when a reasonable
mapping exists. Provider-specific content (audio, video, document,
pre-tokenised input) is reached through :class:`ExtendedEmbeddingPart`.

Note: an adapter whose backend doesn't support a given universal type
(e.g. OpenAI rejects images) must surface a typed error rather than
silently dropping the input.
"""


# ===========================================================================
# Request-level configuration
# ===========================================================================


TaskType = Literal[
    "semantic_similarity",
    "classification",
    "clustering",
    "retrieval_document",
    "retrieval_query",
    "question_answering",
    "fact_verification",
    "code_retrieval_query",
]
"""Normalised embedding task-type values.

Lowercased from Google's documented vocabulary (``SEMANTIC_SIMILARITY``,
etc.). Some embedding models produce different vectors depending on the
task they're being used for; setting this hint lets the model optimise
appropriately. Currently honoured only by Google GenAI; OpenAI and
Transformers adapters ignore it.
"""


class ExtendedEmbeddingConfig(BaseModel):
    """Provider-specific request-level configuration knobs.

    All fields are optional. Each is honoured by exactly one provider
    family (or one path within a provider). Adapters silently ignore
    knobs they don't understand rather than erroring — these are hints,
    not contracts.
    """

    task_type: TaskType | None = Field(
        default=None,
        description="Hint to embedding models that vary their output by intended use. Honoured by Google GenAI; ignored by others.",
    )
    title: str | None = Field(
        default=None,
        description="Document title hint, only meaningful when ``task_type='retrieval_document'``. Google-only.",
    )
    auto_truncate: bool | None = Field(
        default=None,
        description="Allow the server to truncate over-long inputs instead of erroring. Google Vertex only.",
    )
    document_ocr: bool | None = Field(
        default=None,
        description="Apply OCR to document inputs. Google Vertex + ``gemini-embedding-2`` family only.",
    )
    audio_track_extraction: bool | None = Field(
        default=None,
        description="Extract and embed audio tracks from video inputs. Google Vertex + ``gemini-embedding-2`` family only.",
    )
    user: str | None = Field(
        default=None,
        description="End-user identifier for abuse monitoring. OpenAI-only.",
    )
    raw: dict[str, Any] | None = Field(
        default=None,
        description="Raw provider-specific config that doesn't fit the typed knobs above. Adapter-defined contract.",
    )


# ===========================================================================
# Output: per-input metadata payloads
# ===========================================================================


class PerInputUsage(BaseModel):
    """Per-embedding token / character usage telemetry.

    Populated by adapters whose providers report per-input metrics:
    Google Vertex (``ContentEmbeddingStatistics.token_count`` and
    ``truncated``). OpenAI reports usage at the request level only — its
    adapter populates :class:`EmbeddingUsage`, not this. Transformers
    reports nothing.
    """

    token_count: int | None = Field(
        default=None,
        ge=0,
        description="Tokens consumed for this input, when reported.",
    )
    character_count: int | None = Field(
        default=None,
        ge=0,
        description="Characters consumed for this input, when the provider counts characters instead of tokens.",
    )
    truncated: bool | None = Field(
        default=None,
        description="True if the provider truncated the input to fit the model's context window.",
    )


class AlignedModalityVector(BaseModel):
    """A secondary embedding vector for the same input in a different modality.

    Models like CLIP, SigLIP, BLIP, and Vertex
    ``multimodalembedding@001`` project text and images into a *shared*
    embedding space, allowing cross-modal retrieval. When a caller embeds
    a single image with such a model, the model can also produce the
    aligned text-projection vector (and vice versa). This payload exposes
    the secondary vector; the primary vector lives on
    :attr:`Embedding.vector`.

    Adapters whose models don't produce aligned vectors leave this
    empty.
    """

    modality: Literal["text", "image", "audio", "video"] = Field(
        ...,
        description="Modality this aligned vector represents.",
    )
    vector: list[float] = Field(
        ...,
        min_length=1,
        description="The aligned embedding vector in the named modality.",
    )


class PerTokenVectors(BaseModel):
    """Per-token embedding vectors for a single input.

    Populated by adapters whose backends naturally produce per-token
    vectors (Transformers ``last_hidden_state`` from BERT-family models;
    Wav2Vec2 / Whisper per-frame outputs). Only emitted when the caller
    explicitly requests them; pooling to a single vector for
    :attr:`Embedding.vector` is the default.

    Adapters for API-based providers (OpenAI, Google) leave this empty.
    """

    vectors: list[list[float]] = Field(
        ...,
        min_length=1,
        description="One embedding vector per token in input order.",
    )
    tokens: list[str] | None = Field(
        default=None,
        description="Token strings parallel to ``vectors``, when the tokenizer exposes them.",
    )


class ExtendedEmbeddingMetadata(BaseModel):
    """Provider-specific metadata attached to a single :class:`Embedding`.

    Mirror of the chat-side extended wrapper concept, but as a struct of
    optional payloads rather than a discriminated union — multiple kinds
    of metadata can coexist for one embedding (e.g. CLIP returns an
    aligned secondary vector AND adapter-side per-input usage).

    Adapters populate whichever fields are meaningful for the input and
    leave the rest as ``None``.
    """

    usage: PerInputUsage | None = Field(
        default=None,
        description="Per-embedding token/character usage and truncation flag.",
    )
    aligned_vectors: list[AlignedModalityVector] | None = Field(
        default=None,
        description="Secondary aligned vectors for cross-modal retrieval (CLIP-style models).",
    )
    per_token_vectors: PerTokenVectors | None = Field(
        default=None,
        description="Per-token embedding vectors for this input, when requested and available.",
    )
    raw: dict[str, Any] | None = Field(
        default=None,
        description="Provider-specific metadata that doesn't fit the typed fields above.",
    )


# ===========================================================================
# Output: per-input embedding and full response
# ===========================================================================


class Embedding(BaseModel):
    """A single embedding vector produced for one input.

    The ``index`` correlates back to the position of the originating
    :data:`EmbeddingPart` in the request's ``inputs`` list, even if the
    adapter parallelises calls or reorders results internally.
    """

    index: int = Field(
        ...,
        ge=0,
        description="Position of the originating input in the request's 'inputs' list.",
    )
    vector: list[float] = Field(
        ...,
        min_length=1,
        description="The embedding vector. Length matches the model's default dimensionality or the requested 'output_dimensions'.",
    )
    extended: ExtendedEmbeddingMetadata | None = Field(
        default=None,
        description="Provider-specific per-input metadata (usage, aligned vectors, per-token vectors).",
    )


class EmbeddingUsage(BaseModel):
    """Request-level usage telemetry.

    OpenAI reports ``input_tokens``; Google Vertex reports
    ``input_characters`` (counting characters not tokens) and only at the
    Vertex backend. Gemini API and Transformers report neither. Adapters
    populate whichever is available and leave the rest as ``None``.
    """

    input_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Total tokens consumed across all inputs in the request, when the provider counts tokens.",
    )
    input_characters: int | None = Field(
        default=None,
        ge=0,
        description="Total characters consumed across all inputs in the request, when the provider counts characters.",
    )


class EmbedResponse(BaseModel):
    """Response from an embedding request.

    Carries one :class:`Embedding` per input in the request's ``inputs``
    list (in input order — the adapter is responsible for preserving
    correspondence). Some providers (Google Vertex
    multimodalembedding) naturally produce multiple vectors per *call*
    but conceptually one per *input*; the adapter splits inputs
    accordingly to maintain the one-vector-per-input contract.
    """

    model: str = Field(
        ...,
        min_length=1,
        description="Provider-side model name that produced the embeddings.",
    )
    embeddings: list[Embedding] = Field(
        ...,
        description="One Embedding per input, in input order.",
    )
    usage: EmbeddingUsage | None = Field(
        default=None,
        description="Request-level usage telemetry, when the provider reports it.",
    )


# ===========================================================================
# Adapter signature helper
# ===========================================================================


OutputDimensions = Annotated[
    PositiveInt | None,
    Field(
        default=None,
        description=(
            "Truncate vectors to this dimensionality (Matryoshka). "
            "Supported by OpenAI text-embedding-3 family and Google "
            "gemini-embedding-2 family; ignored elsewhere."
        ),
    ),
]
"""Type alias adapters use for their ``output_dimensions`` parameter.

Embeddings can be produced at the model's default dimensionality or
truncated to a smaller size (Matryoshka). Both OpenAI (``dimensions``)
and Google (``output_dimensionality``) support this on their newer
models. Transformers does not support runtime truncation; the caller
truncates client-side.
"""
