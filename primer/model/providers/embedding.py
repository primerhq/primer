"""Embedding (vector-embedding) provider configuration.

Defines :class:`EmbeddingProvider` and the discriminated set of backend
connection configs it can carry. ``GoogleConfig`` (the Gemini connection
shape) is shared with the LLM family and imported from there.
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar

from pydantic import BaseModel, Field, SecretStr

from primer.model.common import Identifiable
from primer.model.providers._shared import Limits, _HttpApiKeyConfig
from primer.model.providers.llm import GoogleConfig


class EmbeddingProviderType(str, Enum):
    """Supported embedding provider backends.

    The string value is what gets serialized in configuration files, so it
    must remain stable across releases.
    """

    HUGGINGFACE = "huggingface"
    OPENAI = "openai"
    GEMINI = "gemini"


class OpenAIEmbeddingFlavor(str, Enum):
    """Which OpenAI-compatible embedding server is on the other end of the wire.

    The wire protocol is the same across these flavors (the official
    ``openai`` SDK speaks to all of them) but server-side expectations
    differ enough that the adapter benefits from knowing which one it
    is talking to. Examples of flavor-specific behavior:

    * Real OpenAI rejects empty ``api_key`` with 401.
    * LM Studio tolerates empty ``api_key`` (unauthenticated by default)
      but accepts non-empty keys (e.g. for a reverse proxy that enforces
      its own auth).

    Use :attr:`OTHER` for any OpenAI-compatible embedding endpoint that
    is not explicitly modelled — the adapter will treat it conservatively
    and require an api_key.
    """

    OPENAI = "openai"
    LMSTUDIO = "lmstudio"
    OTHER = "other"


class OpenAIConfig(_HttpApiKeyConfig):
    """Connection settings for the OpenAI embedding provider.

    Carries a :attr:`flavor` discriminator so the adapter can apply
    server-specific quirk handling (e.g. tolerate empty ``api_key``
    for LM Studio) without proliferating enum variants on
    :class:`EmbeddingProviderType` for every OpenAI-compatible endpoint.
    """

    flavor: OpenAIEmbeddingFlavor = Field(
        default=OpenAIEmbeddingFlavor.OTHER,
        description=(
            "Identifies which OpenAI-compatible server is on the other "
            "end so the adapter can apply flavor-specific behavior. "
            "Defaults to OTHER (conservative; api_key required)."
        ),
    )


class HuggingFaceConfig(BaseModel):
    """Connection settings for the HuggingFace embedding provider."""

    token: SecretStr = Field(
        ...,
        description="HuggingFace token used to pull the transformer model.",
    )


class EmbeddingModel(BaseModel):
    """A single embedding model exposed by a provider.

    Carries only the provider-side model identifier. Vector
    dimensionality is intentionally NOT recorded here -- it is learned
    by :class:`primer.ingest.DocumentIngester` at run time from the
    actual length of the first chunk's embedding vector and propagated
    to :meth:`primer.int.VectorStore.create_collection`. Recording the
    dimension twice (registry + actual) was a redundancy that could
    silently drift; deferring the answer to ingestion time keeps the
    registry schema-free.
    """

    name: str = Field(
        ...,
        min_length=1,
        description="Provider-side model identifier (e.g. 'text-embedding-3-small').",
    )


class EmbeddingProvider(Identifiable):
    """A configured embedding provider entry.

    One ``EmbeddingProvider`` describes a single vector-embedding backend the
    application can route requests to. The ``id`` (inherited from
    :class:`Identifiable`) is a user-chosen handle; the ``config`` field is
    a discriminated set of provider-specific configurations; the matching
    variant must agree with ``provider``.
    """

    _id_prefix: ClassVar[str] = "embedding-provider"

    provider: EmbeddingProviderType = Field(
        ...,
        description="Which embedding provider backend this entry targets.",
    )
    models: list[EmbeddingModel] = Field(
        ...,
        min_length=1,
        description="Models permitted on this provider; must contain at least one.",
    )
    config: OpenAIConfig | HuggingFaceConfig | GoogleConfig = Field(
        ...,
        description="Backend-specific connection configuration; must match ``provider``.",
    )
    limits: Limits = Field(
        ...,
        description="Rate-limit settings enforced when calling this provider.",
    )
