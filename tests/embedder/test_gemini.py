"""Unit tests for the Gemini embedder adapter."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from primer.embedder.gemini import (
    GeminiEmbedder,
    _extract_embed_config,
    _part_to_text,
    _translate_response,
)
from primer.model.chat import AudioPart, DocumentPart, ImagePart, VideoPart
from primer.model.embedding import (
    EmbedResponse,
    Embedding,
    EmbeddingUsage,
    ExtendedEmbeddingConfig,
    ExtendedEmbeddingPart,
    TextPart,
    TokensPart,
)
from primer.model.except_ import (
    AuthenticationError,
    ConfigError,
    ModelNotFoundError,
    UnsupportedContentError,
)
from primer.model.provider import (
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingProviderType,
    GoogleConfig,
    Limits,
)


def _make_provider(
    *,
    api_key: str = "api-key-test",
    models: list[str] | None = None,
    max_concurrency: int = 4,
) -> EmbeddingProvider:
    return EmbeddingProvider(
        id="gemini-emb-default",
        provider=EmbeddingProviderType.GEMINI,
        models=[
            EmbeddingModel(name=name)
            for name in (models or ["text-embedding-004"])
        ],
        config=GoogleConfig(api_key=SecretStr(api_key)),
        limits=Limits(max_concurrency=max_concurrency),
    )


class TestConstructor:
    def test_accepts_valid_config(self) -> None:
        provider = _make_provider()
        embedder = GeminiEmbedder(provider)
        assert embedder._client is None

    def test_accepts_empty_api_key(self) -> None:
        """Mirror of GeminiLLM: api_key is optional at construction so
        unauthenticated proxies are wireable; real Gemini surfaces 401
        at call time when a key is required."""
        provider = _make_provider(api_key="")
        embedder = GeminiEmbedder(provider)
        assert embedder._client is None

    def test_rejects_wrong_provider_type(self) -> None:
        provider = _make_provider()
        object.__setattr__(provider, "provider", "openai")  # type: ignore[arg-type]
        with pytest.raises(ConfigError, match="GEMINI"):
            GeminiEmbedder(provider)

    def test_rejects_wrong_config_type(self) -> None:
        from pydantic import HttpUrl
        from primer.model.provider import OpenAIConfig

        provider = EmbeddingProvider(
            id="x",
            provider=EmbeddingProviderType.GEMINI,
            models=[EmbeddingModel(name="m")],
            config=OpenAIConfig(  # type: ignore[arg-type]
                url=HttpUrl("https://x/v1/"),
                api_key=SecretStr("sk-x"),
            ),
            limits=Limits(max_concurrency=1),
        )
        with pytest.raises(ConfigError, match="GoogleConfig"):
            GeminiEmbedder(provider)

    def test_initialises_rate_limiter_and_max_concurrency(self) -> None:
        provider = _make_provider(max_concurrency=3)
        embedder = GeminiEmbedder(provider)
        assert embedder._rate_limiter is not None
        assert embedder._max_concurrency == 3
        assert embedder._rate_limit_key == "embedder:gemini-emb-default"

    def test_logs_init(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="primer.embedder.gemini")
        provider = _make_provider(models=["text-embedding-004"], max_concurrency=2)
        GeminiEmbedder(provider)
        records = [r for r in caplog.records if "Gemini embedder initialized" in r.message]
        assert len(records) == 1
        assert records[0].provider_id == "gemini-emb-default"  # type: ignore[attr-defined]


class TestListModels:
    async def test_returns_configured_names(self) -> None:
        provider = _make_provider(models=["m1", "m2"])
        embedder = GeminiEmbedder(provider)
        assert list(await embedder.list_models()) == ["m1", "m2"]

    async def test_does_not_call_upstream(self) -> None:
        provider = _make_provider()
        embedder = GeminiEmbedder(provider)
        with patch.object(GeminiEmbedder, "_get_client") as m:
            await embedder.list_models()
            m.assert_not_called()


class TestPartToText:
    def test_text_part(self) -> None:
        assert _part_to_text(TextPart(text="hi")) == "hi"

    def test_image_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="text-only"):
            _part_to_text(ImagePart(url="https://x/i"))

    def test_audio_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="audio"):
            _part_to_text(ExtendedEmbeddingPart(extended=AudioPart(url="https://x/a")))

    def test_video_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="video"):
            _part_to_text(ExtendedEmbeddingPart(extended=VideoPart(url="https://x/v")))

    def test_document_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="document"):
            _part_to_text(ExtendedEmbeddingPart(extended=DocumentPart(url="https://x/d")))

    def test_tokens_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="pre-tokenised"):
            _part_to_text(ExtendedEmbeddingPart(extended=TokensPart(tokens=[1, 2])))


class TestExtractEmbedConfig:
    def test_none_when_nothing_set(self) -> None:
        assert _extract_embed_config(None, None) is None

    def test_output_dimensions_only(self) -> None:
        cfg = _extract_embed_config(512, None)
        assert cfg is not None
        assert cfg.output_dimensionality == 512

    def test_task_type_uppercased(self) -> None:
        cfg = _extract_embed_config(None, ExtendedEmbeddingConfig(task_type="retrieval_query"))
        assert cfg.task_type == "RETRIEVAL_QUERY"

    def test_title_forwarded(self) -> None:
        cfg = _extract_embed_config(None, ExtendedEmbeddingConfig(title="My Doc"))
        assert cfg.title == "My Doc"

    def test_auto_truncate_forwarded(self) -> None:
        cfg = _extract_embed_config(None, ExtendedEmbeddingConfig(auto_truncate=True))
        assert cfg.auto_truncate is True

    def test_document_ocr_forwarded(self) -> None:
        cfg = _extract_embed_config(None, ExtendedEmbeddingConfig(document_ocr=True))
        assert cfg.document_ocr is True

    def test_audio_track_extraction_forwarded(self) -> None:
        cfg = _extract_embed_config(None, ExtendedEmbeddingConfig(audio_track_extraction=True))
        assert cfg.audio_track_extraction is True

    def test_combined(self) -> None:
        cfg = _extract_embed_config(
            512,
            ExtendedEmbeddingConfig(task_type="classification", title="T"),
        )
        assert cfg.output_dimensionality == 512
        assert cfg.task_type == "CLASSIFICATION"
        assert cfg.title == "T"


class TestTranslateResponse:
    def test_no_metadata_no_statistics(self) -> None:
        resp = NS(
            embeddings=[NS(values=[0.1, 0.2, 0.3], statistics=None)],
            metadata=None,
        )
        out = _translate_response("m", resp)
        assert isinstance(out, EmbedResponse)
        assert out.model == "m"
        assert out.usage is None
        assert len(out.embeddings) == 1
        assert out.embeddings[0].vector == [0.1, 0.2, 0.3]
        assert out.embeddings[0].extended is None
        assert out.embeddings[0].index == 0

    def test_batch_preserves_order(self) -> None:
        resp = NS(
            embeddings=[
                NS(values=[0.1], statistics=None),
                NS(values=[0.2], statistics=None),
                NS(values=[0.3], statistics=None),
            ],
            metadata=None,
        )
        out = _translate_response("m", resp)
        assert [e.index for e in out.embeddings] == [0, 1, 2]
        assert [e.vector for e in out.embeddings] == [[0.1], [0.2], [0.3]]

    def test_with_statistics_populates_per_input_usage(self) -> None:
        resp = NS(
            embeddings=[NS(values=[0.1], statistics=NS(token_count=42, truncated=False))],
            metadata=None,
        )
        out = _translate_response("m", resp)
        assert out.embeddings[0].extended is not None
        assert out.embeddings[0].extended.usage is not None
        assert out.embeddings[0].extended.usage.token_count == 42
        assert out.embeddings[0].extended.usage.truncated is False

    def test_metadata_billable_chars_to_usage(self) -> None:
        resp = NS(
            embeddings=[NS(values=[0.1], statistics=None)],
            metadata=NS(billable_character_count=100),
        )
        out = _translate_response("m", resp)
        assert isinstance(out.usage, EmbeddingUsage)
        assert out.usage.input_characters == 100
        assert out.usage.input_tokens is None


def _patched_client(monkeypatch: pytest.MonkeyPatch):
    instance = MagicMock()
    instance.aio = MagicMock()
    instance.aio.models = MagicMock()
    instance.aio.models.embed_content = AsyncMock()
    cls_mock = MagicMock(return_value=instance)
    monkeypatch.setattr("primer.embedder.gemini.genai.Client", cls_mock)
    return cls_mock, instance


def _ok_response():
    return NS(
        embeddings=[NS(values=[0.1, 0.2], statistics=None)],
        metadata=NS(billable_character_count=2),
    )


class TestEmbed:
    async def test_unknown_model_raises(self) -> None:
        provider = _make_provider(models=["m1"])
        embedder = GeminiEmbedder(provider)
        with pytest.raises(ModelNotFoundError, match="not-real"):
            await embedder.embed(model="not-real", inputs=[TextPart(text="hi")])

    async def test_basic_embed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _make_provider(models=["m1"])
        embedder = GeminiEmbedder(provider)
        _, instance = _patched_client(monkeypatch)
        instance.aio.models.embed_content.return_value = _ok_response()
        out = await embedder.embed(model="m1", inputs=[TextPart(text="hi")])
        assert out.model == "m1"
        assert out.embeddings[0].vector == [0.1, 0.2]

    async def test_unsupported_input_raises_before_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider(models=["m1"])
        embedder = GeminiEmbedder(provider)
        _, instance = _patched_client(monkeypatch)
        with pytest.raises(UnsupportedContentError):
            await embedder.embed(model="m1", inputs=[ImagePart(url="https://x/i")])
        instance.aio.models.embed_content.assert_not_called()

    async def test_request_payload_includes_config_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider(models=["m1"])
        embedder = GeminiEmbedder(provider)
        _, instance = _patched_client(monkeypatch)
        instance.aio.models.embed_content.return_value = _ok_response()
        await embedder.embed(
            model="m1",
            inputs=[TextPart(text="hi")],
            output_dimensions=256,
            config=ExtendedEmbeddingConfig(task_type="retrieval_query"),
        )
        kwargs = instance.aio.models.embed_content.call_args.kwargs
        assert kwargs["model"] == "m1"
        assert kwargs["contents"] == ["hi"]
        assert kwargs["config"] is not None
        assert kwargs["config"].output_dimensionality == 256
        assert kwargs["config"].task_type == "RETRIEVAL_QUERY"

    async def test_request_payload_omits_config_when_nothing_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider(models=["m1"])
        embedder = GeminiEmbedder(provider)
        _, instance = _patched_client(monkeypatch)
        instance.aio.models.embed_content.return_value = _ok_response()
        await embedder.embed(model="m1", inputs=[TextPart(text="hi")])
        kwargs = instance.aio.models.embed_content.call_args.kwargs
        assert kwargs["config"] is None


class TestExceptionWrapping:
    async def test_authentication_classified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from google.genai import errors as gerrors
        provider = _make_provider(models=["m1"])
        embedder = GeminiEmbedder(provider)
        _, instance = _patched_client(monkeypatch)
        instance.aio.models.embed_content.side_effect = gerrors.APIError(
            401, {"error": {"code": 401, "message": "auth fail", "status": "TEST"}}
        )
        with pytest.raises(AuthenticationError):
            await embedder.embed(model="m1", inputs=[TextPart(text="hi")])


class TestConcurrency:
    async def test_semaphore_serialises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _make_provider(models=["m1"], max_concurrency=1)
        embedder = GeminiEmbedder(provider)
        _, instance = _patched_client(monkeypatch)
        in_flight = 0
        peak = 0

        async def slow_embed(**_kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return _ok_response()

        instance.aio.models.embed_content.side_effect = slow_embed

        async def consume() -> None:
            await embedder.embed(model="m1", inputs=[TextPart(text="hi")])

        await asyncio.gather(consume(), consume(), consume())
        assert peak == 1


class TestPackageReexport:
    def test_reexported(self) -> None:
        import primer.embedder as e
        assert "GeminiEmbedder" in e.__all__
        assert e.GeminiEmbedder is GeminiEmbedder

    def test_others_still_reexported(self) -> None:
        import primer.embedder as e
        assert "OpenAIEmbedder" in e.__all__
        assert "HuggingFaceEmbedder" in e.__all__
