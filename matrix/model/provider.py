"""Pydantic models describing LLM and embedding provider configuration.

These types define how providers are declared in configuration: which backend
they talk to, which models are permitted, the provider-specific connection
details, and the rate limits the application should enforce against them.

Two top-level provider kinds are supported:

* :class:`LLMProvider` — chat / completion backends.
* :class:`EmbeddingProvider` — vector-embedding backends.
"""

from enum import Enum

from pydantic import BaseModel, Field, HttpUrl, PositiveInt, SecretStr

from matrix.model.common import Identifiable


class LLMProviderType(str, Enum):
    """Supported LLM provider backends.

    The string value is what gets serialized in configuration files, so it
    must remain stable across releases.
    """

    OPENRESPONSES = "openresponses"


class EmbeddingProviderType(str, Enum):
    """Supported embedding provider backends.

    The string value is what gets serialized in configuration files, so it
    must remain stable across releases.
    """

    HUGGINGFACE = "huggingface"
    OPENAI = "openai"


class OpenResponsesFlavor(str, Enum):
    """Which OpenAI-compatible server is on the other end of the wire.

    The wire protocol is the same across these flavors (the official
    ``openai`` SDK speaks to all of them) but server-side quirks differ
    enough that the adapter benefits from knowing which one it is talking
    to. Examples of flavor-specific behavior observed in the project's
    research probes:

    * LM Studio suppresses reasoning events when ``store=True``.
    * LM Studio leaves ``encrypted_content`` absent on reasoning items.
    * Real OpenAI requires a non-empty ``api_key``; LM Studio tolerates one.

    Use :attr:`OTHER` for any OpenAI-compatible endpoint that is not
    explicitly modelled (Ollama, vLLM, llama.cpp, Together, OpenRouter,
    etc.) — the adapter will treat it conservatively and apply no
    flavor-specific optimisations.
    """

    OPENAI = "openai"
    LMSTUDIO = "lmstudio"
    OTHER = "other"


class _HttpApiKeyConfig(BaseModel):
    """Shared shape for HTTP providers authenticated by an API key.

    Not intended to be used directly; subclass it for each concrete provider
    so that type checkers can keep the providers distinct even when their
    connection fields are identical.
    """

    url: HttpUrl = Field(
        ...,
        description="Base URL of the provider's HTTP endpoint.",
    )
    api_key: SecretStr = Field(
        ...,
        description="API key used to authenticate against the provider.",
    )


class OpenResponsesConfig(_HttpApiKeyConfig):
    """Connection settings for the ``openresponses`` LLM provider.

    Carries a :attr:`flavor` discriminator so the adapter can apply
    server-specific quirk handling without proliferating provider
    enum values for every OpenAI-compatible endpoint variant.
    """

    flavor: OpenResponsesFlavor = Field(
        default=OpenResponsesFlavor.OTHER,
        description="Identifies which OpenAI-compatible server is on the other end so the adapter can apply flavor-specific behavior. Defaults to OTHER (conservative; no quirk handling applied).",
    )


class OpenAIConfig(_HttpApiKeyConfig):
    """Connection settings for the OpenAI embedding provider."""


class HuggingFaceConfig(BaseModel):
    """Connection settings for the HuggingFace embedding provider."""

    token: SecretStr = Field(
        ...,
        description="HuggingFace token used to pull the transformer model.",
    )


class LLMModel(BaseModel):
    """A single LLM model exposed by a provider."""

    name: str = Field(
        ...,
        min_length=1,
        description="Provider-side model identifier (e.g. 'gpt-4o-mini').",
    )
    context_length: PositiveInt = Field(
        ...,
        description="Maximum number of tokens the model accepts in a request.",
    )


class EmbeddingModel(BaseModel):
    """A single embedding model exposed by a provider."""

    name: str = Field(
        ...,
        min_length=1,
        description="Provider-side model identifier (e.g. 'text-embedding-3-small').",
    )
    length: PositiveInt = Field(
        ...,
        description="Dimensionality of the embedding vector the model returns.",
    )


class Limits(BaseModel):
    """Rate-limit settings the client must respect for a provider."""

    max_concurrency: PositiveInt = Field(
        ...,
        description="Maximum number of in-flight requests allowed at once.",
    )


class LLMProvider(Identifiable):
    """A configured LLM provider entry.

    One ``LLMProvider`` describes a single chat/completion backend the
    application can route requests to. The ``id`` (inherited from
    :class:`Identifiable`) is a user-chosen handle; ``provider`` selects the
    backend implementation; ``config`` carries backend-specific connection
    details; ``models`` and ``limits`` constrain what the application is
    allowed to send.
    """

    provider: LLMProviderType = Field(
        ...,
        description="Which LLM provider backend this entry targets.",
    )
    models: list[LLMModel] = Field(
        ...,
        min_length=1,
        description="Models permitted on this provider; must contain at least one.",
    )
    config: OpenResponsesConfig = Field(
        ...,
        description="Backend-specific connection configuration.",
    )
    limits: Limits = Field(
        ...,
        description="Rate-limit settings enforced when calling this provider.",
    )


class EmbeddingProvider(Identifiable):
    """A configured embedding provider entry.

    One ``EmbeddingProvider`` describes a single vector-embedding backend the
    application can route requests to. The ``id`` (inherited from
    :class:`Identifiable`) is a user-chosen handle; the ``config`` field is
    a discriminated set of provider-specific configurations; the matching
    variant must agree with ``provider``.
    """

    provider: EmbeddingProviderType = Field(
        ...,
        description="Which embedding provider backend this entry targets.",
    )
    models: list[EmbeddingModel] = Field(
        ...,
        min_length=1,
        description="Models permitted on this provider; must contain at least one.",
    )
    config: OpenAIConfig | HuggingFaceConfig = Field(
        ...,
        description="Backend-specific connection configuration; must match ``provider``.",
    )
    limits: Limits = Field(
        ...,
        description="Rate-limit settings enforced when calling this provider.",
    )
