import pytest
from pydantic import ValidationError

from primer.model.web_fetch import (
    ACTIVE_WEB_FETCH_CONFIG_ID,
    RESERVED_WEB_FETCH_IDS,
    ActiveWebFetchConfig,
    AggregatedFetchConfig,
    ExaFetchConfig,
    FirecrawlFetchConfig,
    JinaFetchConfig,
    LocalFetchConfig,
    SingleFetchConfig,
    WebFetchProvider,
    WebFetchProviderType,
)


def test_local_provider_needs_no_key():
    p = WebFetchProvider(
        id="local", provider_type=WebFetchProviderType.LOCAL,
        config=LocalFetchConfig(),
    )
    assert p.config.type is WebFetchProviderType.LOCAL


def test_config_type_must_match_provider_type():
    with pytest.raises(ValidationError):
        WebFetchProvider(
            id="x", provider_type=WebFetchProviderType.FIRECRAWL,
            config=LocalFetchConfig(),
        )


def test_firecrawl_requires_api_key():
    with pytest.raises(ValidationError):
        FirecrawlFetchConfig()  # missing api_key


def test_jina_key_optional():
    assert JinaFetchConfig().api_key is None


def test_single_config_discriminates():
    cfg = ActiveWebFetchConfig(
        id=ACTIVE_WEB_FETCH_CONFIG_ID,
        config=SingleFetchConfig(provider_id="local"),
    )
    assert isinstance(cfg.config, SingleFetchConfig)


def test_aggregated_dedupes_preserving_order():
    cfg = AggregatedFetchConfig(provider_ids=["a", "b", "a", "c"])
    assert cfg.provider_ids == ["a", "b", "c"]


def test_reserved_ids():
    assert "local" in RESERVED_WEB_FETCH_IDS
    assert ACTIVE_WEB_FETCH_CONFIG_ID == "_active_web_fetch_config"
