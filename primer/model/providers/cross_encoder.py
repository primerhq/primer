"""Cross-encoder reranker provider configuration.

Mirrors :class:`primer.model.providers.llm.LLMProvider` and
:class:`primer.model.providers.embedding.EmbeddingProvider` so the
provider registry stays uniform across the three model families.
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar

from pydantic import BaseModel, Field, PositiveInt, SecretStr

from primer.model.common import Identifiable
from primer.model.providers._shared import Limits


class CrossEncoderProviderType(str, Enum):
    """Supported cross-encoder reranker backends.

    The string value is what gets serialized in configuration files, so
    it must remain stable across releases.
    """

    HUGGINGFACE = "huggingface"


class HuggingFaceCrossEncoderConfig(BaseModel):
    """Connection settings for the HuggingFace cross-encoder backend.

    Local sentence-transformers models. Most cross-encoders (BAAI,
    cross-encoder/*, MS-MARCO MiniLM) are public, so the token is
    optional and only needed for gated repos.
    """

    token: SecretStr | None = Field(
        default=None,
        description=(
            "Optional HuggingFace token. Required only if the model "
            "lives in a gated repository; public reranker models like "
            "``BAAI/bge-reranker-v2-m3`` and ``cross-encoder/*`` do not "
            "need it."
        ),
    )


class CrossEncoderModel(BaseModel):
    """A single cross-encoder model exposed by a provider."""

    name: str = Field(
        ...,
        min_length=1,
        description=(
            "Provider-side model identifier "
            "(e.g. 'BAAI/bge-reranker-v2-m3', "
            "'cross-encoder/ms-marco-MiniLM-L-6-v2')."
        ),
    )
    max_pair_length: PositiveInt | None = Field(
        default=None,
        description=(
            "Optional cap on the (query + document) token length the "
            "model will consume per pair. ``None`` defers to the "
            "model's own default; set this when truncation matters."
        ),
    )


class CrossEncoderProvider(Identifiable):
    """A configured cross-encoder reranker provider entry.

    One ``CrossEncoderProvider`` describes a single reranker backend
    the application can route requests to. The ``id`` (inherited from
    :class:`Identifiable`) is a user-chosen handle; ``provider``
    selects the backend implementation; ``config`` carries
    backend-specific connection details; ``models`` and ``limits``
    constrain what the application is allowed to send.

    Mirrors :class:`LLMProvider` and :class:`EmbeddingProvider` so the
    provider registry stays uniform across the three model families.
    """

    _id_prefix: ClassVar[str] = "cross-encoder-provider"

    provider: CrossEncoderProviderType = Field(
        ...,
        description="Which cross-encoder backend this entry targets.",
    )
    models: list[CrossEncoderModel] = Field(
        ...,
        min_length=1,
        description="Models permitted on this provider; must contain at least one.",
    )
    config: HuggingFaceCrossEncoderConfig = Field(
        ...,
        description="Backend-specific connection configuration; must match ``provider``.",
    )
    limits: Limits = Field(
        ...,
        description="Rate-limit settings enforced when calling this provider.",
    )
