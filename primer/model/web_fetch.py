"""Pydantic models for the web-fetch providers subsystem.

Structurally mirrors primer/model/web_search.py: a CRUD provider row with a
discriminated config union, plus a singleton active-config row discriminated by
``mode`` into single / aggregated. The reserved built-in is the keyless LOCAL
adapter (httpx + trafilatura + docling).
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from primer.model.common import Identifiable


RESERVED_WEB_FETCH_IDS: frozenset[str] = frozenset({"local"})
ACTIVE_WEB_FETCH_CONFIG_ID = "_active_web_fetch_config"


class WebFetchProviderType(str, Enum):
    LOCAL = "local"
    JINA = "jina"
    FIRECRAWL = "firecrawl"
    EXA = "exa"


class LocalFetchConfig(BaseModel):
    """In-process fetch + extract (httpx + trafilatura + docling). No key."""

    type: Literal[WebFetchProviderType.LOCAL] = Field(default=WebFetchProviderType.LOCAL)


class JinaFetchConfig(BaseModel):
    """Jina Reader (r.jina.ai). Works keyless (rate-limited); a key raises limits."""

    type: Literal[WebFetchProviderType.JINA] = Field(default=WebFetchProviderType.JINA)
    api_key: SecretStr | None = Field(
        default=None,
        description="Optional Jina API key; raises rate limits when set.",
    )


class FirecrawlFetchConfig(BaseModel):
    """Firecrawl /v1/scrape (onlyMainContent). Renders JS server-side."""

    type: Literal[WebFetchProviderType.FIRECRAWL] = Field(
        default=WebFetchProviderType.FIRECRAWL,
    )
    api_key: SecretStr = Field(..., description="Firecrawl API key (fc-...).")


class ExaFetchConfig(BaseModel):
    """Exa /contents. Returns page text."""

    type: Literal[WebFetchProviderType.EXA] = Field(default=WebFetchProviderType.EXA)
    api_key: SecretStr = Field(..., description="Exa API key (x-api-key header).")


WebFetchProviderConfig = Annotated[
    LocalFetchConfig | JinaFetchConfig | FirecrawlFetchConfig | ExaFetchConfig,
    Field(discriminator="type"),
]


class WebFetchProvider(Identifiable):
    provider_type: WebFetchProviderType = Field(...)
    config: WebFetchProviderConfig = Field(...)

    @model_validator(mode="after")
    def _validate_config_matches(self) -> "WebFetchProvider":
        expected = {
            WebFetchProviderType.LOCAL: LocalFetchConfig,
            WebFetchProviderType.JINA: JinaFetchConfig,
            WebFetchProviderType.FIRECRAWL: FirecrawlFetchConfig,
            WebFetchProviderType.EXA: ExaFetchConfig,
        }[self.provider_type]
        if not isinstance(self.config, expected):
            raise ValueError(
                f"config kind {type(self.config).__name__} does not match "
                f"provider_type {self.provider_type.value}"
            )
        return self


class WebFetchMode(str, Enum):
    SINGLE = "single"
    AGGREGATED = "aggregated"


class SingleFetchConfig(BaseModel):
    mode: Literal[WebFetchMode.SINGLE] = Field(default=WebFetchMode.SINGLE)
    provider_id: str = Field(..., min_length=1)


class AggregatedFetchConfig(BaseModel):
    mode: Literal[WebFetchMode.AGGREGATED] = Field(default=WebFetchMode.AGGREGATED)
    provider_ids: list[str] = Field(..., min_length=1)

    @field_validator("provider_ids")
    @classmethod
    def _dedupe_preserve_order(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for pid in v:
            if pid not in seen:
                seen.add(pid)
                out.append(pid)
        return out


ActiveFetchConfig = Annotated[
    SingleFetchConfig | AggregatedFetchConfig,
    Field(discriminator="mode"),
]


class ActiveWebFetchConfig(Identifiable):
    config: ActiveFetchConfig = Field(...)


__all__ = [
    "ACTIVE_WEB_FETCH_CONFIG_ID",
    "RESERVED_WEB_FETCH_IDS",
    "ActiveFetchConfig",
    "ActiveWebFetchConfig",
    "AggregatedFetchConfig",
    "ExaFetchConfig",
    "FirecrawlFetchConfig",
    "JinaFetchConfig",
    "LocalFetchConfig",
    "SingleFetchConfig",
    "WebFetchMode",
    "WebFetchProvider",
    "WebFetchProviderConfig",
    "WebFetchProviderType",
]
