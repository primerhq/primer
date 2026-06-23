"""Pydantic models for the web search providers subsystem.

Three groups:

* :class:`WebSearchProvider` — persisted CRUD row carrying a provider
  type enum and a discriminated config union. Mirrors
  :class:`primer.model.provider.SemanticSearchProvider`'s pattern
  (provider type + matching config class enforced by model validator).
* :class:`ActiveWebSearchConfig` — singleton row at id
  :data:`ACTIVE_WEB_SEARCH_CONFIG_ID`. Discriminated by ``mode`` into
  single-provider or aggregated (priority-ordered fallback chain).
* Constants — :data:`RESERVED_WEB_SEARCH_IDS` (the bootstrap-managed
  DuckDuckGo provider id) and the singleton id.

See ``docs/superpowers/specs/2026-06-03-web-search-providers-design.md``
for the full design rationale.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from primer.model.common import Identifiable


# Reserved id of the bootstrap-managed DuckDuckGo provider row.
# Operators cannot create another row at this id (POST 409) and
# cannot delete this row (DELETE 403). Mirrors the SSP "lance" pattern.
RESERVED_WEB_SEARCH_IDS: frozenset[str] = frozenset({"DuckDuckGo"})


# Reserved row id of the singleton active-config row. Underscore-
# prefixed because it's a non-CRUD-exposed singleton (matches
# `_internal_collections_config`).
ACTIVE_WEB_SEARCH_CONFIG_ID = "_active_web_search_config"


# ===========================================================================
# Provider entity
# ===========================================================================


class WebSearchProviderType(str, Enum):
    DUCKDUCKGO = "duckduckgo"
    TAVILY = "tavily"
    FIRECRAWL = "firecrawl"
    EXA = "exa"


class DuckDuckGoConfig(BaseModel):
    """DDG has no API key; defaults to the public web endpoint.

    Present as a class (not None) so the discriminated config union
    can dispatch on the ``type`` field at deserialization time.
    """

    type: Literal[WebSearchProviderType.DUCKDUCKGO] = Field(
        default=WebSearchProviderType.DUCKDUCKGO,
    )


class TavilyConfig(BaseModel):
    """Tavily REST adapter config. Carries the API key as a SecretStr
    so the default ``model_dump()`` redacts it in REST GET/list responses.
    The storage round-trip uses python mode (preserves plaintext).
    """

    type: Literal[WebSearchProviderType.TAVILY] = Field(
        default=WebSearchProviderType.TAVILY,
    )
    api_key: SecretStr = Field(
        ...,
        description=(
            "Tavily API key. Stored as SecretStr so list/get responses "
            "redact the value; the storage round-trip preserves "
            "plaintext (matches LLMProvider pattern)."
        ),
    )


class FirecrawlConfig(BaseModel):
    """Firecrawl REST adapter config. The API key authenticates against
    ``api.firecrawl.dev``; carried as a SecretStr for the same redaction
    behaviour as :class:`TavilyConfig`.
    """

    type: Literal[WebSearchProviderType.FIRECRAWL] = Field(
        default=WebSearchProviderType.FIRECRAWL,
    )
    api_key: SecretStr = Field(
        ...,
        description=(
            "Firecrawl API key (``fc-...``). Stored as SecretStr so "
            "list/get responses redact the value; the storage round-trip "
            "preserves plaintext."
        ),
    )


class ExaConfig(BaseModel):
    """Exa REST adapter config. The API key authenticates against
    ``api.exa.ai`` via the ``x-api-key`` header; carried as a SecretStr.
    """

    type: Literal[WebSearchProviderType.EXA] = Field(
        default=WebSearchProviderType.EXA,
    )
    api_key: SecretStr = Field(
        ...,
        description=(
            "Exa API key. Stored as SecretStr so list/get responses "
            "redact the value; the storage round-trip preserves "
            "plaintext."
        ),
    )


WebSearchProviderConfig = Annotated[
    DuckDuckGoConfig | TavilyConfig | FirecrawlConfig | ExaConfig,
    Field(discriminator="type"),
]


class WebSearchProvider(Identifiable):
    """Persisted provider row.

    ``provider_type`` is a redundant outer copy of ``config.type``,
    kept because it's easier to query/filter on. The model validator
    enforces consistency between the two fields.
    """

    provider_type: WebSearchProviderType = Field(
        ...,
        description="Which web search provider backend this row uses.",
    )
    config: WebSearchProviderConfig = Field(
        ...,
        description=(
            "Backend-specific configuration; ``config.type`` must match "
            "``provider_type``."
        ),
    )

    @model_validator(mode="after")
    def _validate_config_matches(self) -> "WebSearchProvider":
        expected = {
            WebSearchProviderType.DUCKDUCKGO: DuckDuckGoConfig,
            WebSearchProviderType.TAVILY: TavilyConfig,
            WebSearchProviderType.FIRECRAWL: FirecrawlConfig,
            WebSearchProviderType.EXA: ExaConfig,
        }[self.provider_type]
        if not isinstance(self.config, expected):
            raise ValueError(
                f"config kind {type(self.config).__name__} does not "
                f"match provider_type {self.provider_type.value}"
            )
        return self


# ===========================================================================
# Active-config singleton
# ===========================================================================


class WebSearchMode(str, Enum):
    SINGLE = "single"
    AGGREGATED = "aggregated"


class SingleProviderConfig(BaseModel):
    """Single mode: every web-search call routes to one provider.
    Errors propagate; no fallback."""

    mode: Literal[WebSearchMode.SINGLE] = Field(default=WebSearchMode.SINGLE)
    provider_id: str = Field(..., min_length=1)


class AggregatedProviderConfig(BaseModel):
    """Aggregated mode: providers are tried in the order listed in
    ``provider_ids``. On a ``WebSearchUnavailable`` or
    ``WebSearchProviderError`` from one provider, the next is tried.
    Only fails when every provider raises a known-class exception.
    """

    mode: Literal[WebSearchMode.AGGREGATED] = Field(
        default=WebSearchMode.AGGREGATED,
    )
    provider_ids: list[str] = Field(..., min_length=1)

    @field_validator("provider_ids")
    @classmethod
    def _dedupe_preserve_order(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for pid in v:
            if pid in seen:
                continue
            seen.add(pid)
            out.append(pid)
        return out


ActiveProviderConfig = Annotated[
    SingleProviderConfig | AggregatedProviderConfig,
    Field(discriminator="mode"),
]


class ActiveWebSearchConfig(Identifiable):
    """Singleton row at id :data:`ACTIVE_WEB_SEARCH_CONFIG_ID`.

    Holds the discriminated ``config`` union. ``GET /v1/web_search_active_config``
    reads this row; ``PUT`` replaces it. Bootstrap (at app lifespan)
    is the only path that initially creates the row — GET returns
    503 ``subsystem_not_bootstrapped`` if missing.
    """

    config: ActiveProviderConfig = Field(...)


__all__ = [
    "ACTIVE_WEB_SEARCH_CONFIG_ID",
    "ActiveWebSearchConfig",
    "ActiveProviderConfig",
    "AggregatedProviderConfig",
    "DuckDuckGoConfig",
    "ExaConfig",
    "FirecrawlConfig",
    "RESERVED_WEB_SEARCH_IDS",
    "SingleProviderConfig",
    "TavilyConfig",
    "WebSearchMode",
    "WebSearchProvider",
    "WebSearchProviderConfig",
    "WebSearchProviderType",
]
