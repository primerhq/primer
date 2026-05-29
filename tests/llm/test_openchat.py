"""Unit tests for the OpenChat LLM adapter."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from pydantic import HttpUrl, SecretStr

from primer.llm.openchat import OpenChatLLM, _POLICY_BY_FLAVOR, _FlavorPolicy
from primer.model.except_ import ConfigError
from primer.model.provider import (
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    OpenChatConfig,
    OpenChatFlavor,
)


def _make_provider(
    *,
    flavor: OpenChatFlavor = OpenChatFlavor.OPENAI,
    api_key: str | None = "sk-test",
    models: list[str] | None = None,
    max_concurrency: int = 4,
    url: str = "https://api.openai.com/v1/",
) -> LLMProvider:
    return LLMProvider(
        id="openchat-default",
        provider=LLMProviderType.OPENCHAT,
        models=[
            LLMModel(name=name, context_length=8192)
            for name in (models or ["gpt-4o-mini"])
        ],
        config=OpenChatConfig(
            url=HttpUrl(url),
            api_key=SecretStr(api_key) if api_key is not None else None,
            flavor=flavor,
        ),
        limits=Limits(max_concurrency=max_concurrency),
    )


class TestFlavorPolicy:
    def test_openai_policy_requires_api_key(self) -> None:
        assert _POLICY_BY_FLAVOR[OpenChatFlavor.OPENAI].require_api_key is True

    def test_lmstudio_policy_tolerates_no_key(self) -> None:
        assert _POLICY_BY_FLAVOR[OpenChatFlavor.LMSTUDIO].require_api_key is False

    def test_ollama_policy_tolerates_no_key(self) -> None:
        assert _POLICY_BY_FLAVOR[OpenChatFlavor.OLLAMA].require_api_key is False

    def test_vllm_policy_tolerates_no_key(self) -> None:
        assert _POLICY_BY_FLAVOR[OpenChatFlavor.VLLM].require_api_key is False

    def test_other_policy_requires_api_key(self) -> None:
        assert _POLICY_BY_FLAVOR[OpenChatFlavor.OTHER].require_api_key is True

    def test_policy_dataclass_is_frozen(self) -> None:
        policy = _POLICY_BY_FLAVOR[OpenChatFlavor.OPENAI]
        with pytest.raises(Exception):
            policy.require_api_key = False  # type: ignore[misc]


class TestConstructor:
    def test_accepts_valid_openai_config(self) -> None:
        llm = OpenChatLLM(_make_provider(flavor=OpenChatFlavor.OPENAI))
        assert llm._policy is _POLICY_BY_FLAVOR[OpenChatFlavor.OPENAI]
        assert llm._client is None

    def test_accepts_lmstudio_with_no_key(self) -> None:
        llm = OpenChatLLM(
            _make_provider(
                flavor=OpenChatFlavor.LMSTUDIO,
                api_key=None,
                url="http://localhost:1234/v1/",
            )
        )
        assert llm._policy.require_api_key is False

    def test_accepts_ollama_with_no_key(self) -> None:
        llm = OpenChatLLM(
            _make_provider(
                flavor=OpenChatFlavor.OLLAMA,
                api_key=None,
                url="http://localhost:11434/v1/",
            )
        )
        assert llm._policy.require_api_key is False

    def test_accepts_vllm_with_no_key(self) -> None:
        llm = OpenChatLLM(
            _make_provider(
                flavor=OpenChatFlavor.VLLM,
                api_key=None,
                url="http://localhost:8000/v1/",
            )
        )
        assert llm._policy.require_api_key is False

    def test_rejects_empty_api_key_for_openai_flavor(self) -> None:
        with pytest.raises(ConfigError, match="api_key is required"):
            OpenChatLLM(_make_provider(flavor=OpenChatFlavor.OPENAI, api_key=""))

    def test_rejects_missing_api_key_for_other_flavor(self) -> None:
        with pytest.raises(ConfigError, match="api_key is required"):
            OpenChatLLM(
                _make_provider(
                    flavor=OpenChatFlavor.OTHER,
                    api_key=None,
                    url="https://api.example.com/v1/",
                )
            )

    def test_rejects_wrong_provider_type(self) -> None:
        provider = _make_provider()
        object.__setattr__(provider, "provider", LLMProviderType.OPENRESPONSES)
        with pytest.raises(ConfigError, match="OPENCHAT"):
            OpenChatLLM(provider)

    def test_logs_init_with_structured_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="primer.llm.openchat")
        OpenChatLLM(
            _make_provider(models=["gpt-4o-mini", "gpt-4o"], max_concurrency=2)
        )
        records = [r for r in caplog.records if "OpenChat adapter" in r.message]
        assert len(records) == 1
        record = records[0]
        assert record.provider_id == "openchat-default"  # type: ignore[attr-defined]
        assert record.flavor == "openai"  # type: ignore[attr-defined]
        assert record.models == ["gpt-4o-mini", "gpt-4o"]  # type: ignore[attr-defined]
        assert record.max_concurrency == 2  # type: ignore[attr-defined]


class TestListModels:
    async def test_returns_configured_model_names(self) -> None:
        llm = OpenChatLLM(_make_provider(models=["gpt-4o-mini", "gpt-4o"]))
        assert list(await llm.list_models()) == ["gpt-4o-mini", "gpt-4o"]

    async def test_does_not_call_upstream(self) -> None:
        llm = OpenChatLLM(_make_provider())
        with patch.object(OpenChatLLM, "_get_client") as mock_get_client:
            await llm.list_models()
            mock_get_client.assert_not_called()


import base64

from primer.llm.openchat import _part_to_content
from primer.model.chat import (
    AudioPart,
    DocumentPart,
    ExtendedPart,
    ImagePart,
    TextPart,
    VideoPart,
)
from primer.model.except_ import UnsupportedContentError


class TestPartToContent:
    def test_text_part(self) -> None:
        assert _part_to_content(TextPart(text="hi")) == {
            "type": "text",
            "text": "hi",
        }

    def test_image_part_url(self) -> None:
        out = _part_to_content(ImagePart(url="https://example.com/x.png"))
        assert out == {
            "type": "image_url",
            "image_url": {"url": "https://example.com/x.png"},
        }

    def test_image_part_url_includes_detail_when_set(self) -> None:
        out = _part_to_content(ImagePart(url="https://example.com/x.png", detail="high"))
        assert out == {
            "type": "image_url",
            "image_url": {"url": "https://example.com/x.png", "detail": "high"},
        }

    def test_image_part_data_emits_data_uri(self) -> None:
        out = _part_to_content(ImagePart(data=b"\x89PNG", mime_type="image/png"))
        assert out["type"] == "image_url"
        url = out["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == b"\x89PNG"

    def test_image_part_data_defaults_mime(self) -> None:
        out = _part_to_content(ImagePart(data=b"raw"))
        assert out["image_url"]["url"].startswith("data:application/octet-stream;base64,")

    def test_image_part_file_id_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="file_id"):
            _part_to_content(ImagePart(file_id="file-abc"))

    def test_document_part_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="document"):
            _part_to_content(
                DocumentPart(data=b"%PDF", mime_type="application/pdf")
            )

    def test_audio_extended_part_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="audio"):
            _part_to_content(
                ExtendedPart(extended=AudioPart(data=b"x", mime_type="audio/mp3"))
            )

    def test_video_extended_part_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="video"):
            _part_to_content(
                ExtendedPart(extended=VideoPart(url="https://example.com/v.mp4"))
            )
