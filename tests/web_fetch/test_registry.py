from pydantic import SecretStr

from primer.api.registries.web_fetch_registry import default_web_fetch_factory
from primer.model.web_fetch import (
    ExaFetchConfig, FirecrawlFetchConfig, JinaFetchConfig, LocalFetchConfig,
    WebFetchProvider, WebFetchProviderType,
)
from primer.web_fetch.exa import ExaAdapter
from primer.web_fetch.firecrawl import FirecrawlAdapter
from primer.web_fetch.jina import JinaAdapter
from primer.web_fetch.local import LocalAdapter


def _p(pt, cfg):
    return WebFetchProvider(id="x", provider_type=pt, config=cfg)


def test_factory_dispatch():
    assert isinstance(default_web_fetch_factory(_p(WebFetchProviderType.LOCAL, LocalFetchConfig())), LocalAdapter)
    assert isinstance(default_web_fetch_factory(_p(WebFetchProviderType.JINA, JinaFetchConfig())), JinaAdapter)
    assert isinstance(default_web_fetch_factory(_p(WebFetchProviderType.FIRECRAWL, FirecrawlFetchConfig(api_key=SecretStr("fc")))), FirecrawlAdapter)
    assert isinstance(default_web_fetch_factory(_p(WebFetchProviderType.EXA, ExaFetchConfig(api_key=SecretStr("exa")))), ExaAdapter)
