"""LLM (chat / completion) provider configuration.

Defines :class:`LLMProvider` and the discriminated set of backend
connection configs it can carry. ``GoogleConfig`` is defined here (the
Gemini connection shape) and is also reused by the embedding family.
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, PositiveInt, SecretStr, model_validator

from primer.model.common import Identifiable
from primer.model.providers._shared import Limits, _HttpApiKeyConfig


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
    OpenRouter config with the extra field dropped. Sibling LLM
    configs do not need this because their ``url``/``flavor`` fields
    already distinguish them; ``OpenRouterConfig``'s only field that
    overlaps with another arm is ``api_key``, which is present on
    every config.
    """

    # extra='forbid': see class docstring. Defends the validator
    # against mismatched-shape config dicts.
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


class LLMProvider(Identifiable):
    """A configured LLM provider entry.

    One ``LLMProvider`` describes a single chat/completion backend the
    application can route requests to. The ``id`` (inherited from
    :class:`Identifiable`) is a user-chosen handle; ``provider`` selects the
    backend implementation; ``config`` carries backend-specific connection
    details; ``models`` and ``limits`` constrain what the application is
    allowed to send.
    """

    _id_prefix: ClassVar[str] = "llm-provider"

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
