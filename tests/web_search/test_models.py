"""Pydantic round-trip + discriminator tests for the web search models."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from primer.model.web_search import (
    ACTIVE_WEB_SEARCH_CONFIG_ID,
    RESERVED_WEB_SEARCH_IDS,
    ActiveWebSearchConfig,
    AggregatedProviderConfig,
    DuckDuckGoConfig,
    SingleProviderConfig,
    TavilyConfig,
    WebSearchMode,
    WebSearchProvider,
    WebSearchProviderType,
)


# ---------- Provider model ----------------------------------------


class TestWebSearchProvider:
    def test_reserved_ids_contain_duckduckgo(self) -> None:
        assert "DuckDuckGo" in RESERVED_WEB_SEARCH_IDS
        assert isinstance(RESERVED_WEB_SEARCH_IDS, frozenset)

    def test_duckduckgo_provider_round_trip(self) -> None:
        row = WebSearchProvider(
            id="DuckDuckGo",
            provider_type=WebSearchProviderType.DUCKDUCKGO,
            config=DuckDuckGoConfig(),
        )
        dumped = row.model_dump(mode="json")
        assert dumped["id"] == "DuckDuckGo"
        assert dumped["provider_type"] == "duckduckgo"
        assert dumped["config"]["type"] == "duckduckgo"

    def test_tavily_provider_round_trip_redacts_secret(self) -> None:
        row = WebSearchProvider(
            id="tavily-prod",
            provider_type=WebSearchProviderType.TAVILY,
            config=TavilyConfig(api_key=SecretStr("tvly-secret-XXXX")),
        )
        dumped = row.model_dump(mode="json")
        # SecretStr redacts in default model_dump.
        assert dumped["config"]["api_key"] != "tvly-secret-XXXX"
        assert "***" in dumped["config"]["api_key"]

    def test_tavily_round_trip_preserves_plaintext_in_python_mode(self) -> None:
        """The storage round-trip uses python mode so the plaintext
        is preserved across write→read."""
        row = WebSearchProvider(
            id="tavily-prod",
            provider_type=WebSearchProviderType.TAVILY,
            config=TavilyConfig(api_key=SecretStr("tvly-secret-XXXX")),
        )
        # In python mode, SecretStr is preserved (not redacted to ***).
        as_python = row.model_dump()
        assert as_python["config"]["api_key"].get_secret_value() == "tvly-secret-XXXX"

    def test_discriminator_dispatches_tavily_dict_to_tavily_config(self) -> None:
        body = {
            "id": "tavily-prod",
            "provider_type": "tavily",
            "config": {"type": "tavily", "api_key": "tvly-secret"},
        }
        row = WebSearchProvider.model_validate(body)
        assert isinstance(row.config, TavilyConfig)
        assert not isinstance(row.config, DuckDuckGoConfig)

    def test_discriminator_dispatches_duckduckgo_dict(self) -> None:
        body = {
            "id": "DuckDuckGo",
            "provider_type": "duckduckgo",
            "config": {"type": "duckduckgo"},
        }
        row = WebSearchProvider.model_validate(body)
        assert isinstance(row.config, DuckDuckGoConfig)

    def test_provider_type_mismatch_raises(self) -> None:
        with pytest.raises(ValidationError):
            WebSearchProvider(
                id="bad",
                provider_type=WebSearchProviderType.TAVILY,
                config=DuckDuckGoConfig(),
            )

    def test_provider_type_enum_values(self) -> None:
        assert WebSearchProviderType.DUCKDUCKGO.value == "duckduckgo"
        assert WebSearchProviderType.TAVILY.value == "tavily"
        assert WebSearchProviderType.FIRECRAWL.value == "firecrawl"
        assert WebSearchProviderType.EXA.value == "exa"

    def test_firecrawl_provider_round_trip_redacts_secret(self) -> None:
        from primer.model.web_search import FirecrawlConfig

        row = WebSearchProvider(
            id="firecrawl-prod",
            provider_type=WebSearchProviderType.FIRECRAWL,
            config=FirecrawlConfig(api_key=SecretStr("fc-secret-XXXX")),
        )
        dumped = row.model_dump(mode="json")
        assert "fc-secret-XXXX" not in str(dumped)
        assert dumped["config"]["type"] == "firecrawl"
        # python mode preserves the SecretStr.
        as_python = row.model_dump()
        assert as_python["config"]["api_key"].get_secret_value() == "fc-secret-XXXX"

    def test_exa_provider_round_trip_redacts_secret(self) -> None:
        from primer.model.web_search import ExaConfig

        row = WebSearchProvider(
            id="exa-prod",
            provider_type=WebSearchProviderType.EXA,
            config=ExaConfig(api_key=SecretStr("exa-secret-XXXX")),
        )
        dumped = row.model_dump(mode="json")
        assert "exa-secret-XXXX" not in str(dumped)
        assert dumped["config"]["type"] == "exa"
        as_python = row.model_dump()
        assert as_python["config"]["api_key"].get_secret_value() == "exa-secret-XXXX"

    def test_firecrawl_discriminator_dispatch(self) -> None:
        from primer.model.web_search import FirecrawlConfig

        body = {
            "id": "fc",
            "provider_type": "firecrawl",
            "config": {"type": "firecrawl", "api_key": "fc-x"},
        }
        row = WebSearchProvider.model_validate(body)
        assert isinstance(row.config, FirecrawlConfig)

    def test_exa_discriminator_dispatch(self) -> None:
        from primer.model.web_search import ExaConfig

        body = {
            "id": "exa",
            "provider_type": "exa",
            "config": {"type": "exa", "api_key": "exa-x"},
        }
        row = WebSearchProvider.model_validate(body)
        assert isinstance(row.config, ExaConfig)

    def test_firecrawl_provider_type_mismatch_raises(self) -> None:
        from primer.model.web_search import FirecrawlConfig

        with pytest.raises(ValidationError):
            WebSearchProvider(
                id="bad",
                provider_type=WebSearchProviderType.EXA,
                config=FirecrawlConfig(api_key=SecretStr("fc-x")),
            )


# ---------- Active-config singleton -------------------------------


class TestActiveWebSearchConfig:
    def test_singleton_id_constant(self) -> None:
        assert ACTIVE_WEB_SEARCH_CONFIG_ID == "_active_web_search_config"

    def test_mode_enum_values(self) -> None:
        assert WebSearchMode.SINGLE.value == "single"
        assert WebSearchMode.AGGREGATED.value == "aggregated"

    def test_single_mode_round_trip(self) -> None:
        cfg = ActiveWebSearchConfig(
            id=ACTIVE_WEB_SEARCH_CONFIG_ID,
            config=SingleProviderConfig(provider_id="DuckDuckGo"),
        )
        dumped = cfg.model_dump(mode="json")
        assert dumped["config"]["mode"] == "single"
        assert dumped["config"]["provider_id"] == "DuckDuckGo"

    def test_aggregated_mode_round_trip(self) -> None:
        cfg = ActiveWebSearchConfig(
            id=ACTIVE_WEB_SEARCH_CONFIG_ID,
            config=AggregatedProviderConfig(
                provider_ids=["Tavily-prod", "DuckDuckGo"],
            ),
        )
        dumped = cfg.model_dump(mode="json")
        assert dumped["config"]["mode"] == "aggregated"
        assert dumped["config"]["provider_ids"] == ["Tavily-prod", "DuckDuckGo"]

    def test_discriminator_dispatches_single_dict(self) -> None:
        body = {
            "id": ACTIVE_WEB_SEARCH_CONFIG_ID,
            "config": {"mode": "single", "provider_id": "DuckDuckGo"},
        }
        row = ActiveWebSearchConfig.model_validate(body)
        assert isinstance(row.config, SingleProviderConfig)
        assert not isinstance(row.config, AggregatedProviderConfig)

    def test_discriminator_dispatches_aggregated_dict(self) -> None:
        body = {
            "id": ACTIVE_WEB_SEARCH_CONFIG_ID,
            "config": {"mode": "aggregated", "provider_ids": ["A", "B"]},
        }
        row = ActiveWebSearchConfig.model_validate(body)
        assert isinstance(row.config, AggregatedProviderConfig)

    def test_aggregated_empty_provider_ids_raises(self) -> None:
        with pytest.raises(ValidationError):
            AggregatedProviderConfig(provider_ids=[])

    def test_aggregated_dedupes_preserving_order(self) -> None:
        cfg = AggregatedProviderConfig(
            provider_ids=["A", "B", "A", "C", "B"],
        )
        assert cfg.provider_ids == ["A", "B", "C"]

    def test_single_provider_id_min_length_one(self) -> None:
        with pytest.raises(ValidationError):
            SingleProviderConfig(provider_id="")
