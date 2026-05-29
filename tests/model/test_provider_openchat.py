"""Schema round-trip tests for the OpenChat provider config."""

from __future__ import annotations

import pytest
from pydantic import HttpUrl, SecretStr, ValidationError

from primer.model.provider import (
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    OpenChatConfig,
    OpenChatFlavor,
)


class TestOpenChatEnum:
    def test_openchat_enum_value_present(self) -> None:
        assert LLMProviderType.OPENCHAT.value == "openchat"

    def test_openchat_enum_round_trips_through_string(self) -> None:
        assert LLMProviderType("openchat") is LLMProviderType.OPENCHAT


class TestOpenChatFlavor:
    @pytest.mark.parametrize(
        "value",
        ["openai", "lmstudio", "ollama", "vllm", "other"],
    )
    def test_flavor_values_exposed(self, value: str) -> None:
        assert OpenChatFlavor(value).value == value

    def test_flavor_default_is_other(self) -> None:
        config = OpenChatConfig(url=HttpUrl("http://localhost:1234/v1/"))
        assert config.flavor is OpenChatFlavor.OTHER


class TestOpenChatConfig:
    def test_minimal_config_with_no_api_key(self) -> None:
        config = OpenChatConfig(
            url=HttpUrl("http://localhost:1234/v1/"),
            flavor=OpenChatFlavor.LMSTUDIO,
        )
        assert str(config.url).startswith("http://localhost:1234")
        assert config.api_key is None
        assert config.flavor is OpenChatFlavor.LMSTUDIO

    def test_config_with_api_key_round_trips(self) -> None:
        config = OpenChatConfig(
            url=HttpUrl("https://api.openai.com/v1/"),
            api_key=SecretStr("sk-test"),
            flavor=OpenChatFlavor.OPENAI,
        )
        assert config.api_key.get_secret_value() == "sk-test"
        assert config.flavor is OpenChatFlavor.OPENAI

    def test_url_required(self) -> None:
        with pytest.raises(ValidationError):
            OpenChatConfig()  # type: ignore[call-arg]


class TestLLMProviderUnionAcceptsOpenChat:
    def test_create_llm_provider_row_with_openchat_config(self) -> None:
        provider = LLMProvider(
            id="lmstudio-local",
            provider=LLMProviderType.OPENCHAT,
            models=[LLMModel(name="local-model", context_length=8192)],
            config=OpenChatConfig(
                url=HttpUrl("http://localhost:1234/v1/"),
                flavor=OpenChatFlavor.LMSTUDIO,
            ),
            limits=Limits(max_concurrency=2),
        )
        assert provider.provider is LLMProviderType.OPENCHAT
        assert isinstance(provider.config, OpenChatConfig)

    def test_json_round_trip(self) -> None:
        provider = LLMProvider(
            id="openai-chat",
            provider=LLMProviderType.OPENCHAT,
            models=[LLMModel(name="gpt-4o-mini", context_length=128_000)],
            config=OpenChatConfig(
                url=HttpUrl("https://api.openai.com/v1/"),
                api_key=SecretStr("sk-test"),
                flavor=OpenChatFlavor.OPENAI,
            ),
            limits=Limits(max_concurrency=4),
        )
        dumped = provider.model_dump(mode="json")
        assert dumped["provider"] == "openchat"
        assert dumped["config"]["flavor"] == "openai"
        assert dumped["config"]["url"].startswith("https://api.openai.com/v1")
        dumped["config"]["api_key"] = "sk-test"
        roundtrip = LLMProvider.model_validate(dumped)
        assert roundtrip.provider is LLMProviderType.OPENCHAT
        assert isinstance(roundtrip.config, OpenChatConfig)
        assert roundtrip.config.flavor is OpenChatFlavor.OPENAI
