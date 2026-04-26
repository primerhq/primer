"""Pydantic models describing LLM, embedding, and toolset provider configuration.

These types define how providers are declared in configuration: which
backend they talk to, which models are permitted, the provider-specific
connection details, and the rate limits the application should enforce
against them.

Three top-level provider kinds are supported:

* :class:`LLMProvider` — chat / completion backends.
* :class:`EmbeddingProvider` — vector-embedding backends.
* :class:`Toolset` — tool sources (internal registry or MCP server).
"""

from enum import Enum

from pydantic import BaseModel, Field, HttpUrl, PositiveInt, SecretStr, model_validator

from matrix.model.common import Identifiable


class LLMProviderType(str, Enum):
    """Supported LLM provider backends.

    The string value is what gets serialized in configuration files, so it
    must remain stable across releases.
    """

    OPENRESPONSES = "openresponses"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"


class EmbeddingProviderType(str, Enum):
    """Supported embedding provider backends.

    The string value is what gets serialized in configuration files, so it
    must remain stable across releases.
    """

    HUGGINGFACE = "huggingface"
    OPENAI = "openai"
    GEMINI = "gemini"


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


class GoogleConfig(BaseModel):
    """Connection settings for the Gemini LLM provider.

    Targets the Gemini API (Google AI Studio) — single api_key auth.
    Vertex AI uses a different auth model (GCP application default
    credentials + project/location) and warrants its own provider type
    if needed.
    """

    api_key: SecretStr = Field(
        ...,
        description="Gemini API key from Google AI Studio.",
    )


class AnthropicConfig(BaseModel):
    """Connection settings for the Anthropic LLM provider.

    Targets the Anthropic API — single api_key auth. AWS Bedrock and
    GCP Vertex variants warrant their own provider types if needed.
    """
    api_key: SecretStr = Field(
        ...,
        description="Anthropic API key.",
    )


class OllamaConfig(BaseModel):
    """Connection settings for the Ollama LLM provider.

    Targets a local or remote Ollama HTTP server. ``api_key`` is
    optional — Ollama has no authentication by default but users may
    deploy it behind a reverse proxy that enforces its own auth.
    """
    url: HttpUrl = Field(
        ...,
        description="Base URL of the Ollama HTTP endpoint (e.g. http://localhost:11434).",
    )
    api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Optional bearer token forwarded as the Authorization header. "
            "Useful when running Ollama behind a reverse proxy that enforces auth."
        ),
    )


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
    config: OpenResponsesConfig | GoogleConfig | AnthropicConfig | OllamaConfig = Field(
        ...,
        description="Backend-specific connection configuration; must match ``provider``.",
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
    config: OpenAIConfig | HuggingFaceConfig | GoogleConfig = Field(
        ...,
        description="Backend-specific connection configuration; must match ``provider``.",
    )
    limits: Limits = Field(
        ...,
        description="Rate-limit settings enforced when calling this provider.",
    )


# ===========================================================================
# Toolset configuration (tool sources: internal registry or MCP server)
# ===========================================================================


class ToolsetProviderType(str, Enum):
    """Supported toolset provider backends.

    The string value is what gets serialized in configuration files, so it
    must remain stable across releases.
    """

    INTERNAL = "internal"
    MCP = "mcp"


class TransportType(str, Enum):
    """Transport mechanisms supported by the MCP toolset provider."""

    STDIO = "stdio"
    HTTP = "http"


class StdioConfig(BaseModel):
    """Stdio transport for an MCP server — launches a subprocess and
    speaks the MCP protocol over its stdin/stdout.
    """

    command: list[str] = Field(
        ...,
        min_length=1,
        description="Command (argv) to launch the MCP server subprocess.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables to set when launching the subprocess.",
    )


class HttpConfig(BaseModel):
    """HTTP transport for an MCP server — connects to a remote endpoint
    speaking MCP over HTTP (e.g. SSE / streamable HTTP).
    """

    url: str = Field(
        ...,
        min_length=1,
        description="Base URL of the remote MCP server endpoint.",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="HTTP headers to include on every request to the MCP server (e.g. Authorization).",
    )


class McpConfig(BaseModel):
    """Connection settings for an MCP toolset provider.

    Carries a :attr:`transport` discriminator selecting how the client
    talks to the MCP server, and a :attr:`config` field holding the
    transport-specific details. The two MUST agree — a model validator
    enforces consistency.
    """

    transport: TransportType = Field(
        ...,
        description="Which transport mechanism the MCP client uses to talk to the server.",
    )
    config: StdioConfig | HttpConfig = Field(
        ...,
        description="Transport-specific connection details. Must match ``transport``.",
    )

    @model_validator(mode="after")
    def _validate_config_matches_transport(self) -> "McpConfig":
        if self.transport == TransportType.STDIO and not isinstance(self.config, StdioConfig):
            raise ValueError(
                "transport='stdio' requires a StdioConfig in 'config'"
            )
        if self.transport == TransportType.HTTP and not isinstance(self.config, HttpConfig):
            raise ValueError(
                "transport='http' requires an HttpConfig in 'config'"
            )
        return self


class Toolset(Identifiable):
    """A configured tool source.

    Conceptually a *set of tools* the application can offer to LLMs.
    Tools themselves are not enumerated here — they are resolved at
    runtime from the provider:

    * ``internal`` — tools registered in the application's internal
      registry (looked up by toolset ``id``). No additional config is
      needed; ``config`` MUST be ``None``.
    * ``mcp`` — tools queried from an MCP server. ``config`` MUST be an
      :class:`McpConfig` describing the transport and its connection
      details.

    The ``id`` (inherited from :class:`Identifiable`) is a user-chosen
    handle the application uses to refer to this toolset.
    """

    provider: ToolsetProviderType = Field(
        ...,
        description="Which toolset provider backend this entry targets.",
    )
    config: McpConfig | None = Field(
        default=None,
        description="Provider-specific config. Required for 'mcp', must be omitted for 'internal'.",
    )

    @model_validator(mode="after")
    def _validate_config_matches_provider(self) -> "Toolset":
        if self.provider == ToolsetProviderType.MCP and self.config is None:
            raise ValueError(
                "provider='mcp' requires a McpConfig in 'config'"
            )
        if self.provider == ToolsetProviderType.INTERNAL and self.config is not None:
            raise ValueError(
                "provider='internal' must not have a 'config'"
            )
        return self
