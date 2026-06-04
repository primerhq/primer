"""Schema tests for the OpenRouter LLM provider type and config.

Pins:

* :class:`OpenRouterConfig` parsing — required ``api_key``,
  ``HttpUrl`` validation on ``app_url``, and the two optional
  attribution fields.
* :class:`LLMProvider` dispatch via the
  ``_coerce_config_to_provider`` validator — the ``openrouter`` arm
  resolves to :class:`OpenRouterConfig`, and a mismatched config
  shape is rejected.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.provider import (
    LLMProvider,
    LLMProviderType,
    Limits,
    LLMModel,
    OpenRouterConfig,
)


class TestOpenRouterConfig:
    def test_parses_with_api_key_only(self):
        cfg = OpenRouterConfig(api_key="sk-or-v1-abc")
        assert cfg.api_key.get_secret_value() == "sk-or-v1-abc"
        assert cfg.app_name is None
        assert cfg.app_url is None

    def test_parses_with_attribution(self):
        cfg = OpenRouterConfig(
            api_key="sk-or-v1-abc",
            app_name="primer-staging",
            app_url="https://primer.example",
        )
        assert cfg.app_name == "primer-staging"
        assert str(cfg.app_url).startswith("https://primer.example")

    def test_rejects_missing_api_key(self):
        with pytest.raises(ValidationError):
            OpenRouterConfig()

    def test_rejects_malformed_app_url(self):
        with pytest.raises(ValidationError):
            OpenRouterConfig(api_key="sk-or-v1-abc", app_url="not a url")


class TestLLMProviderOpenRouterDispatch:
    def _row(self, **overrides):
        body = {
            "id": "or-1",
            "provider": "openrouter",
            "config": {"api_key": "sk-or-v1-abc"},
            "models": [{"name": "anthropic/claude-3.5-sonnet", "context_length": 200000}],
            "limits": {"max_concurrency": 4},
        }
        body.update(overrides)
        return body

    def test_round_trip(self):
        row = LLMProvider.model_validate(self._row())
        assert row.provider == LLMProviderType.OPENROUTER
        assert isinstance(row.config, OpenRouterConfig)
        assert row.config.api_key.get_secret_value() == "sk-or-v1-abc"
        dumped = row.model_dump(mode="json")
        re_parsed = LLMProvider.model_validate(dumped)
        assert isinstance(re_parsed.config, OpenRouterConfig)

    def test_rejects_mismatched_config_shape(self):
        with pytest.raises(ValidationError):
            LLMProvider.model_validate(self._row(
                config={"url": "https://x.example/v1", "api_key": "k"},
            ))
