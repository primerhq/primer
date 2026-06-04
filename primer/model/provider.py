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

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, PositiveInt, SecretStr, model_validator

from primer.model.common import Identifiable


class LLMProviderType(str, Enum):
    """Supported LLM provider backends.

    The string value is what gets serialized in configuration files, so it
    must remain stable across releases.
    """

    OPENRESPONSES = "openresponses"
    OPENCHAT = "openchat"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    OPENROUTER = "openrouter"


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


class OpenChatFlavor(str, Enum):
    """Which OpenAI-compatible Chat Completions server is on the wire.

    The legacy ``/v1/chat/completions`` surface is the dominant one for
    non-OpenAI servers (LM Studio, Ollama's OpenAI shim, vLLM,
    OpenRouter, Together, Fireworks). Distinguishing the flavor lets the
    adapter apply server-specific defaults — chiefly whether an
    ``api_key`` is required at construction time.

    Use :attr:`OTHER` for any OpenAI-compatible Chat Completions
    endpoint not explicitly modelled.
    """

    OPENAI = "openai"
    LMSTUDIO = "lmstudio"
    OLLAMA = "ollama"
    VLLM = "vllm"
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

    ``api_key`` is optional at the schema level so operators can register
    self-hosted endpoints (LM Studio, vLLM, llama.cpp server, a sidecar
    proxy that injects auth) that don't require a bearer token. Adapters
    that talk to providers which *do* require auth surface a 401 from
    the upstream provider at call time, which is the natural place for
    that error to manifest.
    """

    url: HttpUrl = Field(
        ...,
        description="Base URL of the provider's HTTP endpoint.",
    )
    api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Optional API key forwarded as the Authorization bearer. "
            "Leave unset for unauthenticated endpoints (LM Studio, "
            "self-hosted vLLM, etc.); the upstream provider will return "
            "401 at call time if it actually requires authentication."
        ),
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


class OpenChatConfig(_HttpApiKeyConfig):
    """Connection settings for the ``openchat`` LLM provider.

    Targets the legacy OpenAI Chat Completions wire format
    (``/v1/chat/completions``) — the surface every OpenAI-compatible
    third party (LM Studio, Ollama-OpenAI shim, vLLM, OpenRouter, …)
    supports.
    """

    flavor: OpenChatFlavor = Field(
        default=OpenChatFlavor.OTHER,
        description=(
            "Identifies which OpenAI-compatible Chat Completions server "
            "is on the other end so the adapter can apply flavor-specific "
            "behavior. Defaults to OTHER (conservative; api_key required)."
        ),
    )


class GoogleConfig(BaseModel):
    """Connection settings for the Gemini LLM provider.

    Targets the Gemini API (Google AI Studio) — single api_key auth.
    Vertex AI uses a different auth model (GCP application default
    credentials + project/location) and warrants its own provider type
    if needed.

    ``api_key`` is optional at the schema level so operators can wire a
    proxy that injects auth elsewhere; without one, calls to the real
    Gemini API surface 401 at call time.
    """

    api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Gemini API key from Google AI Studio. Optional — leave "
            "unset only when the endpoint is fronted by something that "
            "supplies auth."
        ),
    )


class AnthropicConfig(BaseModel):
    """Connection settings for the Anthropic LLM provider.

    Targets the Anthropic API — single api_key auth. AWS Bedrock and
    GCP Vertex variants warrant their own provider types if needed.

    ``api_key`` is optional at the schema level so operators can wire a
    proxy that injects auth elsewhere; without one, calls to the real
    Anthropic API surface 401 at call time.
    """
    api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Anthropic API key. Optional — leave unset only when the "
            "endpoint is fronted by something that supplies auth."
        ),
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


class OpenRouterConfig(BaseModel):
    """OpenRouter provider configuration.

    OpenRouter is a unified gateway in front of many upstream LLM
    providers (Anthropic, OpenAI, Google, Mistral, ...) exposing a
    drop-in OpenAI-compatible /chat/completions endpoint. The base
    URL is fixed (https://openrouter.ai/api/v1); only the API key is
    required. The two attribution fields are optional; when set, the
    adapter sends them as HTTP-Referer and X-Title on every request
    so the deploy shows up on OpenRouter's app leaderboard.

    Does NOT inherit from ``_HttpApiKeyConfig`` because OpenRouter's
    shape is different: no ``url`` field (hard-coded), and ``api_key``
    is required (the upstream is always remote and always
    authenticated).

    Uses ``extra='forbid'`` so a config dict shaped for a different
    provider (e.g. one carrying a ``url`` field) is rejected by the
    LLMProvider validator instead of silently coercing into an
    OpenRouter config with the extra field dropped.
    """

    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(
        ...,
        description=(
            "OpenRouter API key. Required (the OpenRouter base URL is "
            "always remote and always authenticated; a null key is "
            "meaningless)."
        ),
    )
    app_name: str | None = Field(
        default=None,
        description=(
            "Sent as the `X-Title` HTTP header on every request for "
            "OpenRouter app attribution. Optional. When unset the "
            "header is omitted; calls still succeed but do not show "
            "up on OpenRouter's leaderboard surface."
        ),
    )
    app_url: HttpUrl | None = Field(
        default=None,
        description=(
            "Sent as the `HTTP-Referer` HTTP header for OpenRouter "
            "app attribution. Optional. Symmetric to ``app_name``."
        ),
    )


class OAuthClientCredentials(BaseModel):
    """Static OAuth client credentials, for servers that don't support DCR.

    If ``client_secret`` is None the client is treated as a public client
    (PKCE only, no Authorization header on the token endpoint). If
    ``client_secret`` is set the token endpoint is called with HTTP Basic.
    """

    client_id: str = Field(
        ...,
        min_length=1,
        description="OAuth client identifier.",
    )
    client_secret: SecretStr | None = Field(
        default=None,
        description="OAuth client secret. None for public PKCE-only clients.",
    )


class OAuthConfig(BaseModel):
    """OAuth settings for an HTTP MCP server.

    Attached to :class:`HttpConfig` via the optional ``oauth`` field.
    When present, :class:`primer.toolset.mcp.McpToolsetProvider`
    performs Authorization Code + PKCE on a 401 from the MCP endpoint
    instead of immediately failing.
    """

    redirect_uri: HttpUrl = Field(
        ...,
        description=(
            "The OAuth callback URL the application exposes. The auth "
            "server will redirect the user here with ?code=...&state=...; "
            "the application's handler then calls "
            "McpToolsetProvider.complete_oauth(code=..., state=...)."
        ),
    )
    scopes: list[str] = Field(
        default_factory=list,
        description="OAuth scopes to request. Empty list = whatever the server defaults to.",
    )
    resource_uri: str | None = Field(
        default=None,
        description=(
            "RFC 8707 resource indicator. Set on token requests when "
            "the negotiated spec version is 2025-06-18 or 2025-11-25. "
            "If None, defaults to the MCP server's URL (the spec's "
            "recommended canonical resource identifier)."
        ),
    )
    static_client: OAuthClientCredentials | None = Field(
        default=None,
        description=(
            "If set, skip Dynamic Client Registration and use these "
            "credentials. Required when the auth server does not "
            "support DCR; optional otherwise."
        ),
    )
    spec_version: Literal["2025-03-26", "2025-06-18", "2025-11-25"] | None = Field(
        default=None,
        description=(
            "Force a specific MCP authorization spec version. If None, "
            "the adapter probes (newest -> oldest) and picks the first "
            "version the server supports."
        ),
    )
    client_name: str = Field(
        default="primer",
        description="client_name passed during DCR; appears in consent UIs.",
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
    config: (
        OpenResponsesConfig
        | OpenChatConfig
        | GoogleConfig
        | AnthropicConfig
        | OllamaConfig
        | OpenRouterConfig
    ) = Field(
        ...,
        description="Backend-specific connection configuration; must match ``provider``.",
    )
    limits: Limits = Field(
        ...,
        description="Rate-limit settings enforced when calling this provider.",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_config_to_provider(cls, data: object) -> object:
        """Pre-validate: when ``config`` arrives as a dict, parse it with the
        concrete config class matching ``provider`` rather than letting the
        union's first-match-wins behavior pick the wrong subclass.

        ``OpenResponsesConfig`` and ``OpenChatConfig`` share the same
        ``_HttpApiKeyConfig`` shape and overlapping flavor values
        (``openai``, ``other``), so leaving the union to disambiguate
        would silently coerce ``openchat`` configs to
        ``OpenResponsesConfig``.
        """
        if not isinstance(data, dict):
            return data
        provider = data.get("provider")
        config = data.get("config")
        if not isinstance(config, dict):
            return data
        try:
            provider_enum = LLMProviderType(provider) if not isinstance(provider, LLMProviderType) else provider
        except ValueError:
            return data
        config_cls: type[BaseModel] | None = {
            LLMProviderType.OPENRESPONSES: OpenResponsesConfig,
            LLMProviderType.OPENCHAT: OpenChatConfig,
            LLMProviderType.GEMINI: GoogleConfig,
            LLMProviderType.ANTHROPIC: AnthropicConfig,
            LLMProviderType.OLLAMA: OllamaConfig,
            LLMProviderType.OPENROUTER: OpenRouterConfig,
        }.get(provider_enum)
        if config_cls is None:
            return data
        data = {**data, "config": config_cls.model_validate(config)}
        return data


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
# Cross-encoder reranker provider configuration
# ===========================================================================


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
    oauth: OAuthConfig | None = Field(
        default=None,
        description=(
            "If set, the adapter performs OAuth 2.1 (PKCE) on 401 "
            "responses. Any Authorization header in `headers` is "
            "overridden by the OAuth flow's bearer token once obtained."
        ),
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
    harness_id: str | None = Field(
        default=None,
        description=(
            "When set, this row is managed by the named harness. "
            "Mutation through the public CRUD endpoints returns 409 — "
            "use the harness's sync/uninstall flow instead."
        ),
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


# ===========================================================================
# Storage + VectorStore provider configuration
# ===========================================================================


class StorageProviderType(str, Enum):
    """Supported Storage provider backends."""

    POSTGRES = "postgres"
    SQLITE = "sqlite"


# Internal adapter shape; not exposed via API.
# See SemanticSearchProvider for the public-facing entity.
class VectorStoreProviderType(str, Enum):
    """Supported VectorStore provider backends."""

    PGVECTOR = "pgvector"
    PGVECTORSCALE = "pgvectorscale"
    LANCE = "lance"


class PoolConfig(BaseModel):
    """Connection pool settings shared by Postgres-backed providers.

    Maps directly onto asyncpg's :func:`asyncpg.create_pool` parameters.
    Defaults are tuned for a small-to-medium application; large
    deployments should raise ``max_size`` to match expected concurrency.
    """

    min_size: PositiveInt = Field(
        default=1,
        description="Minimum number of connections kept open in the pool.",
    )
    max_size: PositiveInt = Field(
        default=10,
        description="Maximum number of connections the pool will open.",
    )
    acquire_timeout: float = Field(
        default=30.0,
        gt=0,
        description="Seconds a caller will wait to acquire a connection before raising.",
    )
    max_idle: float = Field(
        default=300.0,
        gt=0,
        description="Seconds an idle connection may stay in the pool before being closed.",
    )
    max_lifetime: float = Field(
        default=3600.0,
        gt=0,
        description="Seconds a connection may live before being recycled (defends against leaks).",
    )

    @model_validator(mode="after")
    def _validate_sizes(self) -> "PoolConfig":
        if self.max_size < self.min_size:
            raise ValueError(
                f"max_size ({self.max_size}) must be >= min_size ({self.min_size})"
            )
        return self


class _PostgresBaseConfig(BaseModel):
    """Common Postgres connection fields shared by every Postgres-backed provider.

    Note ``db_schema`` rather than ``schema`` -- ``schema`` would shadow
    Pydantic's deprecated ``BaseModel.schema()`` method.
    """

    hostname: str = Field(
        ...,
        min_length=1,
        description="Postgres host (e.g. 'db.internal' or '127.0.0.1').",
    )
    port: int = Field(
        default=5432,
        ge=1,
        le=65535,
        description="Postgres TCP port.",
    )
    username: str = Field(
        ...,
        min_length=1,
        description="Postgres role to authenticate as.",
    )
    password: SecretStr = Field(
        ...,
        description="Password for the role.",
    )
    database: str = Field(
        ...,
        min_length=1,
        description="Database name to connect to.",
    )
    db_schema: str = Field(
        default="public",
        min_length=1,
        description=(
            "Postgres schema where tables and indexes are created. Renamed "
            "from 'schema' to avoid shadowing Pydantic's BaseModel.schema()."
        ),
    )
    pool: PoolConfig = Field(
        default_factory=PoolConfig,
        description="Connection pool settings.",
    )


class PostgresConfig(_PostgresBaseConfig):
    """Connection settings for the plain Postgres Storage provider.

    No vector extensions required; suitable for the generic CRUD +
    predicate-search :class:`Storage` interface backed by JSONB tables.
    """


class SqliteConfig(BaseModel):
    """Connection settings for the embedded SQLite Storage provider.

    Single-file backend. aiosqlite serialises queries through one
    connection so we expose no pool knobs. WAL mode is the
    recommended default — concurrent readers + single writer.
    """

    path: Path = Field(
        ...,
        description=(
            "Filesystem path to the SQLite database file. Parent "
            "directories are created on demand at initialize() "
            "time. Use a '.sqlite' or '.db' extension by convention."
        ),
    )
    busy_timeout_ms: int = Field(
        default=5000,
        ge=0,
        description=(
            "PRAGMA busy_timeout in milliseconds — how long to wait "
            "when another writer holds the lock before raising "
            "SQLITE_BUSY. 5000 is generous for embedded use."
        ),
    )
    synchronous: Literal["off", "normal", "full"] = Field(
        default="normal",
        description=(
            "PRAGMA synchronous mode. 'normal' is the WAL-recommended "
            "default (one fsync per checkpoint). 'full' = one fsync "
            "per transaction. 'off' = no fsync (risk DB corruption "
            "on power loss)."
        ),
    )
    journal_mode: Literal["wal", "delete", "truncate", "memory"] = Field(
        default="wal",
        description=(
            "PRAGMA journal_mode. 'wal' is the recommended default. "
            "'memory' is for ephemeral test DBs only."
        ),
    )


_DistanceMetric = Literal["cosine", "l2", "ip"]


class _PgVectorBaseConfig(_PostgresBaseConfig):
    """Common HNSW + distance options shared by pgvector-family providers."""

    distance_metric: _DistanceMetric = Field(
        default="cosine",
        description=(
            "Distance metric for the vector index. 'cosine' for normalised "
            "embeddings (most common), 'l2' for Euclidean, 'ip' for inner "
            "product."
        ),
    )
    hnsw_m: PositiveInt = Field(
        default=16,
        description=(
            "HNSW 'm' parameter -- max connections per node. Higher = better "
            "recall, larger index, slower build. pgvector default is 16."
        ),
    )
    hnsw_ef_construction: PositiveInt = Field(
        default=64,
        description=(
            "HNSW 'ef_construction' -- candidate list size during build. "
            "Higher = better recall, slower build. pgvector default is 64."
        ),
    )
    hnsw_ef_search: PositiveInt = Field(
        default=40,
        description=(
            "Query-time 'hnsw.ef_search' GUC -- candidate list size during "
            "queries. Higher = better recall, slower queries. pgvector "
            "default is 40."
        ),
    )
    reindex_cron: str | None = Field(
        default=None,
        description=(
            "Crontab expression scheduling periodic HNSW maintenance via "
            ":meth:`primer.int.VectorStoreProvider.maintain_indexes`. "
            "None disables scheduling (caller drives maintenance manually)."
        ),
    )

    @model_validator(mode="after")
    def _validate_cron(self) -> "_PgVectorBaseConfig":
        if self.reindex_cron is not None:
            try:
                from croniter import croniter
            except ImportError as exc:  # pragma: no cover - dep is in pyproject
                raise ValueError(
                    "reindex_cron is set but croniter is not installed"
                ) from exc
            if not croniter.is_valid(self.reindex_cron):
                raise ValueError(
                    f"reindex_cron {self.reindex_cron!r} is not a valid crontab expression"
                )
        return self


class PgVectorConfig(_PgVectorBaseConfig):
    """Connection settings for the pgvector VectorStore provider.

    Requires the ``vector`` extension to be installable on the target
    database (the provider runs ``CREATE EXTENSION IF NOT EXISTS vector``
    on initialise).
    """


class PgVectorScaleConfig(_PgVectorBaseConfig):
    """Connection settings for the pgvectorscale VectorStore provider.

    Requires the ``vector`` AND ``vectorscale`` extensions. pgvectorscale
    layers on top of pgvector and adds the StreamingDiskANN index, SBQ
    quantization, and tuned HNSW behaviour. When ``enable_diskann`` is
    True the per-collection vector tables get a DiskANN index instead
    of HNSW; the ``diskann_*`` fields below tune that index. When
    ``enable_diskann`` is False the provider behaves exactly like
    :class:`PgVectorConfig` plus the ``vectorscale`` extension being
    installed for opportunistic use.
    """

    enable_diskann: bool = Field(
        default=False,
        description=(
            "When True, create StreamingDiskANN indexes (from "
            "pgvectorscale) instead of pgvector's HNSW. DiskANN is "
            "the right choice for very large collections (10M+ "
            "vectors) where HNSW's memory cost becomes prohibitive."
        ),
    )
    diskann_storage_layout: Literal["memory_optimized", "plain"] = Field(
        default="memory_optimized",
        description=(
            "DiskANN storage layout. ``memory_optimized`` enables "
            "Statistical Binary Quantization (SBQ) -- the default and "
            "the recommended choice; ``plain`` keeps full-precision "
            "vectors in the index."
        ),
    )
    diskann_num_neighbors: PositiveInt = Field(
        default=50,
        description=(
            "DiskANN graph degree -- number of neighbours stored per "
            "node. Higher = better recall, larger index. Default 50."
        ),
    )
    diskann_search_list_size: PositiveInt = Field(
        default=100,
        description=(
            "DiskANN ``search_list_size`` build parameter AND the "
            "default query-time ``diskann.query_search_list_size`` "
            "GUC. Higher = better recall, slower queries / build."
        ),
    )
    diskann_max_alpha: float = Field(
        default=1.2,
        gt=1.0,
        description=(
            "DiskANN graph density. Higher (up to ~1.4) increases "
            "recall at the cost of build time. Default 1.2."
        ),
    )
    diskann_num_bits_per_dimension: PositiveInt | None = Field(
        default=None,
        description=(
            "Bits per dimension used by SBQ when storage_layout is "
            "``memory_optimized``. None lets pgvectorscale pick the "
            "default (typically 2). Ignored when storage_layout is "
            "``plain``."
        ),
    )


class LanceConfig(BaseModel):
    """LanceDB embedded-mode SemanticSearchProvider configuration.

    Persists every collection's vector table as a Lance dataset under
    ``path``. The directory is created with mode 0o700 on first use.
    Multiple LanceDB SSPs can coexist as long as they use different
    paths. Single-process write-safe; multi-process primer-api +
    primer-worker against the same path is out of scope (spec §9).
    """

    path: Path = Field(
        ...,
        description=(
            "Filesystem directory holding the LanceDB datasets. Created "
            "on initialise if missing. Must be writable by the primer "
            "process. Use an absolute path."
        ),
    )
    hnsw_m: PositiveInt = Field(
        default=16,
        description=(
            "HNSW graph degree. Mirrors PgVectorConfig.hnsw_m so the "
            "create modal can share one knobs section across backends."
        ),
    )
    hnsw_ef_construction: PositiveInt = Field(
        default=64,
        description=(
            "HNSW 'ef_construction' -- candidate list size during "
            "index build. Higher = better recall, slower build. "
            "Mirrors PgVectorConfig.hnsw_ef_construction's default."
        ),
    )
    hnsw_ef_search: PositiveInt = Field(
        default=40,
        description=(
            "HNSW query-time candidate list size. Higher = better "
            "recall, slower queries. Mirrors PgVectorConfig.hnsw_ef_search "
            "(the pgvector variant exposes this via the hnsw.ef_search GUC)."
        ),
    )
    index_min_rows: PositiveInt = Field(
        default=1000,
        description=(
            "Skip ANN-index construction until a collection has at least "
            "this many rows. Below the threshold, search runs brute-force."
        ),
    )


class StorageProviderConfig(BaseModel):
    """Top-level Storage provider configuration -- discriminated by ``provider``."""

    provider: StorageProviderType = Field(
        ...,
        description="Which Storage backend to use.",
    )
    config: PostgresConfig | SqliteConfig = Field(
        ...,
        description="Backend-specific connection settings; must match ``provider``.",
    )

    @model_validator(mode="after")
    def _validate_config_matches(self) -> "StorageProviderConfig":
        if self.provider == StorageProviderType.POSTGRES and not isinstance(
            self.config, PostgresConfig
        ):
            raise ValueError(
                "provider='postgres' requires a PostgresConfig in 'config'"
            )
        if self.provider == StorageProviderType.SQLITE and not isinstance(
            self.config, SqliteConfig
        ):
            raise ValueError(
                "provider='sqlite' requires a SqliteConfig in 'config'"
            )
        return self


# Internal adapter shape; not exposed via API.
# See SemanticSearchProvider for the public-facing entity.
class VectorStoreProviderConfig(BaseModel):
    """Top-level VectorStore provider configuration -- discriminated by ``provider``."""

    provider: VectorStoreProviderType = Field(
        ...,
        description="Which VectorStore backend to use.",
    )
    config: PgVectorConfig | PgVectorScaleConfig | LanceConfig = Field(
        ...,
        description="Backend-specific connection settings; must match ``provider``.",
    )

    @model_validator(mode="after")
    def _validate_config_matches(self) -> "VectorStoreProviderConfig":
        expected = {
            VectorStoreProviderType.PGVECTOR: PgVectorConfig,
            VectorStoreProviderType.PGVECTORSCALE: PgVectorScaleConfig,
            VectorStoreProviderType.LANCE: LanceConfig,
        }[self.provider]
        if not isinstance(self.config, expected):
            raise ValueError(
                f"provider={self.provider.value!r} requires a "
                f"{expected.__name__} in 'config'"
            )
        return self


# ===========================================================================
# SemanticSearch provider entity (runtime-CRUD, replaces VectorStoreProviderConfig)
# ===========================================================================


class SemanticSearchProviderType(str, Enum):
    """Supported semantic-search backends.

    Mirrors VectorStoreProviderType (which will be removed once all
    callsites migrate to SemanticSearchProvider).
    """

    PGVECTOR = "pgvector"
    PGVECTORSCALE = "pgvectorscale"
    LANCE = "lance"


class SemanticSearchProvider(Identifiable):
    """Operator-managed semantic-search backend backing collections
    and the internal collections subsystem.

    Stored as a CRUD-able row alongside LLMProvider, EmbeddingProvider,
    etc. The discriminated ``config`` carries backend-specific
    connection + index settings; the parent ``provider`` discriminator
    chooses which config shape is valid.
    """

    provider: SemanticSearchProviderType = Field(
        ...,
        description="Which semantic-search backend to use.",
    )
    config: PgVectorConfig | PgVectorScaleConfig | LanceConfig = Field(
        ...,
        description="Backend-specific connection settings; must match ``provider``.",
    )

    @model_validator(mode="after")
    def _validate_config_matches(self) -> "SemanticSearchProvider":
        expected = {
            SemanticSearchProviderType.PGVECTOR: PgVectorConfig,
            SemanticSearchProviderType.PGVECTORSCALE: PgVectorScaleConfig,
            SemanticSearchProviderType.LANCE: LanceConfig,
        }[self.provider]
        if not isinstance(self.config, expected):
            raise ValueError(
                f"provider={self.provider.value!r} requires a "
                f"{expected.__name__} in 'config'"
            )
        return self
