"""Unit tests for the HuggingFace embedder adapter."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from primer.embedder.huggingface import HuggingFaceEmbedder
from primer.model.except_ import ConfigError
from primer.model.provider import (
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingProviderType,
    HuggingFaceConfig,
    Limits,
)


def _make_provider(
    *,
    token: str = "hf-test-token",
    models: list[str] | None = None,
    max_concurrency: int = 4,
) -> EmbeddingProvider:
    return EmbeddingProvider(
        id="hf-default",
        provider=EmbeddingProviderType.HUGGINGFACE,
        models=[
            EmbeddingModel(name=name)
            for name in (models or ["sentence-transformers/all-MiniLM-L6-v2"])
        ],
        config=HuggingFaceConfig(token=SecretStr(token)),
        limits=Limits(max_concurrency=max_concurrency),
    )


class TestConstructor:
    def test_accepts_valid_config(self) -> None:
        provider = _make_provider()
        embedder = HuggingFaceEmbedder(provider)
        assert embedder._models == {}

    def test_rejects_wrong_provider_type(self) -> None:
        provider = _make_provider()
        object.__setattr__(provider, "provider", "openai")  # type: ignore[arg-type]
        with pytest.raises(ConfigError, match="HUGGINGFACE"):
            HuggingFaceEmbedder(provider)

    def test_rejects_wrong_config_type(self) -> None:
        from pydantic import HttpUrl
        from primer.model.provider import OpenAIConfig

        provider = EmbeddingProvider(
            id="x",
            provider=EmbeddingProviderType.HUGGINGFACE,
            models=[EmbeddingModel(name="m")],
            config=OpenAIConfig(  # type: ignore[arg-type]
                url=HttpUrl("https://api.openai.com/v1/"),
                api_key=SecretStr("sk-x"),
            ),
            limits=Limits(max_concurrency=1),
        )
        with pytest.raises(ConfigError, match="HuggingFaceConfig"):
            HuggingFaceEmbedder(provider)

    def test_initialises_rate_limiter_and_max_concurrency(self) -> None:
        provider = _make_provider(max_concurrency=3)
        embedder = HuggingFaceEmbedder(provider)
        assert embedder._rate_limiter is not None
        assert embedder._max_concurrency == 3
        assert embedder._rate_limit_key == "embedder:hf-default"

    def test_logs_init_with_structured_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="primer.embedder.huggingface")
        provider = _make_provider(models=["a", "b"], max_concurrency=2)
        HuggingFaceEmbedder(provider)
        records = [r for r in caplog.records if "HuggingFace embedder initialized" in r.message]
        assert len(records) == 1
        record = records[0]
        assert record.provider_id == "hf-default"  # type: ignore[attr-defined]
        assert record.models == ["a", "b"]  # type: ignore[attr-defined]
        assert record.max_concurrency == 2  # type: ignore[attr-defined]


class TestListModels:
    async def test_returns_configured_model_names(self) -> None:
        provider = _make_provider(models=["a", "b", "c"])
        embedder = HuggingFaceEmbedder(provider)
        assert list(await embedder.list_models()) == ["a", "b", "c"]

    async def test_does_not_load_model(self) -> None:
        provider = _make_provider()
        embedder = HuggingFaceEmbedder(provider)
        with patch.object(HuggingFaceEmbedder, "_get_model") as m:
            await embedder.list_models()
            m.assert_not_called()


import numpy as np

from primer.embedder.huggingface import (
    _classify_hf_exception,
    _encode_sync,
    _part_to_text,
    _translate_response,
)
from primer.model.chat import (
    AudioPart,
    DocumentPart,
    ImagePart,
    VideoPart,
)
from primer.model.embedding import (
    EmbedResponse,
    Embedding,
    ExtendedEmbeddingPart,
    PerTokenVectors,
    TextPart,
    TokensPart,
)
from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    NetworkError,
    ProviderError,
    UnsupportedContentError,
)


class TestPartToText:
    def test_text_part(self) -> None:
        assert _part_to_text(TextPart(text="hello")) == "hello"

    def test_image_part_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="text-only"):
            _part_to_text(ImagePart(url="https://example.com/img.png"))

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


class TestEncodeSync:
    def test_passes_through_to_model_encode(self) -> None:
        model = MagicMock()
        model.encode.return_value = np.array([[0.1, 0.2]])
        out = _encode_sync(model, ["hi"], "sentence_embedding")
        model.encode.assert_called_once_with(
            ["hi"],
            output_value="sentence_embedding",
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        assert out is model.encode.return_value


class TestTranslateResponse:
    def test_sentence_embedding_no_truncation(self) -> None:
        arrays = np.array([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
        out = _translate_response("m", arrays, "sentence_embedding", None)
        assert isinstance(out, EmbedResponse)
        assert out.model == "m"
        assert out.usage is None
        assert len(out.embeddings) == 2
        assert out.embeddings[0].vector == pytest.approx([0.1, 0.2, 0.3])
        assert out.embeddings[1].vector == pytest.approx([0.4, 0.5, 0.6])
        assert out.embeddings[0].extended is None

    def test_sentence_embedding_with_truncation(self) -> None:
        arrays = np.array([[0.1, 0.2, 0.3, 0.4, 0.5]])
        out = _translate_response("m", arrays, "sentence_embedding", 3)
        assert out.embeddings[0].vector == pytest.approx([0.1, 0.2, 0.3])

    def test_token_embeddings_populates_per_token_vectors(self) -> None:
        arrays = [np.array([[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]])]
        out = _translate_response("m", arrays, "token_embeddings", None)
        assert out.embeddings[0].extended is not None
        assert isinstance(out.embeddings[0].extended.per_token_vectors, PerTokenVectors)
        actual_vecs = out.embeddings[0].extended.per_token_vectors.vectors
        expected_vecs = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
        assert len(actual_vecs) == len(expected_vecs)
        for a, e in zip(actual_vecs, expected_vecs):
            assert a == pytest.approx(e)
        assert out.embeddings[0].vector == pytest.approx([0.3, 0.4])

    def test_token_embeddings_with_truncation(self) -> None:
        arrays = [np.array([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])]
        out = _translate_response("m", arrays, "token_embeddings", 2)
        assert out.embeddings[0].vector == pytest.approx([0.25, 0.35])


class TestClassifyHfException:
    def test_authentication_via_401(self) -> None:
        result = _classify_hf_exception(RuntimeError("401 unauthorized"))
        assert isinstance(result, AuthenticationError)

    def test_authentication_via_class_name(self) -> None:
        class GatedRepoError(Exception):
            pass

        result = _classify_hf_exception(GatedRepoError("gated"))
        assert isinstance(result, AuthenticationError)

    def test_not_found_via_404(self) -> None:
        result = _classify_hf_exception(RuntimeError("404 model not found"))
        assert isinstance(result, BadRequestError)

    def test_oserror_maps_to_network(self) -> None:
        result = _classify_hf_exception(OSError("disk i/o failure"))
        assert isinstance(result, NetworkError)

    def test_other_maps_to_provider(self) -> None:
        result = _classify_hf_exception(ValueError("weird input"))
        assert isinstance(result, ProviderError)
        assert "weird input" in str(result)


from primer.model.embedding import ExtendedEmbeddingConfig
from primer.model.except_ import ModelNotFoundError


def _make_st_mock(arrays_per_call=None):
    mock = MagicMock()
    if arrays_per_call is not None:
        mock.encode.return_value = arrays_per_call
    return mock


def _patched_st(monkeypatch: pytest.MonkeyPatch, arrays):
    instance = _make_st_mock(arrays)
    cls_mock = MagicMock(return_value=instance)
    monkeypatch.setattr("primer.embedder.huggingface.SentenceTransformer", cls_mock)
    return cls_mock, instance


class TestEmbed:
    async def test_unknown_model_raises_model_not_found(self) -> None:
        provider = _make_provider(models=["m1"])
        embedder = HuggingFaceEmbedder(provider)
        with pytest.raises(ModelNotFoundError, match="not-a-model"):
            await embedder.embed(
                model="not-a-model",
                inputs=[TextPart(text="hi")],
            )

    async def test_basic_embed_returns_translated_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider(models=["m1"])
        embedder = HuggingFaceEmbedder(provider)
        _patched_st(monkeypatch, np.array([[0.1, 0.2, 0.3]]))
        out = await embedder.embed(model="m1", inputs=[TextPart(text="hi")])
        assert out.model == "m1"
        assert len(out.embeddings) == 1
        assert out.embeddings[0].vector == pytest.approx([0.1, 0.2, 0.3])
        assert out.usage is None

    async def test_unsupported_input_raises_before_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider(models=["m1"])
        embedder = HuggingFaceEmbedder(provider)
        cls_mock, _ = _patched_st(monkeypatch, np.array([]))
        with pytest.raises(UnsupportedContentError, match="text-only"):
            await embedder.embed(
                model="m1",
                inputs=[ImagePart(url="https://x/i.png")],
            )
        cls_mock.assert_not_called()

    async def test_lazy_load_caches_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider(models=["m1"])
        embedder = HuggingFaceEmbedder(provider)
        cls_mock, instance = _patched_st(monkeypatch, np.array([[0.1]]))
        await embedder.embed(model="m1", inputs=[TextPart(text="a")])
        await embedder.embed(model="m1", inputs=[TextPart(text="b")])
        await embedder.embed(model="m1", inputs=[TextPart(text="c")])
        assert cls_mock.call_count == 1
        assert instance.encode.call_count == 3

    async def test_output_dimensions_truncates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider(models=["m1"])
        embedder = HuggingFaceEmbedder(provider)
        _patched_st(monkeypatch, np.array([[0.1, 0.2, 0.3, 0.4, 0.5]]))
        out = await embedder.embed(
            model="m1",
            inputs=[TextPart(text="hi")],
            output_dimensions=3,
        )
        assert out.embeddings[0].vector == pytest.approx([0.1, 0.2, 0.3])


class TestPerTokenVectors:
    async def test_opt_in_via_extended_raw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider(models=["m1"])
        embedder = HuggingFaceEmbedder(provider)
        _patched_st(monkeypatch, [np.array([[0.1, 0.2], [0.3, 0.4]])])
        out = await embedder.embed(
            model="m1",
            inputs=[TextPart(text="hi")],
            config=ExtendedEmbeddingConfig(raw={"output_value": "token_embeddings"}),
        )
        assert out.embeddings[0].extended is not None
        assert out.embeddings[0].extended.per_token_vectors is not None

    async def test_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _make_provider(models=["m1"])
        embedder = HuggingFaceEmbedder(provider)
        _patched_st(monkeypatch, np.array([[0.1, 0.2]]))
        out = await embedder.embed(model="m1", inputs=[TextPart(text="hi")])
        assert out.embeddings[0].extended is None


class TestExceptionWrapping:
    async def test_authentication_classified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider(models=["m1"])
        embedder = HuggingFaceEmbedder(provider)
        instance = _make_st_mock()
        instance.encode.side_effect = RuntimeError("401 unauthorized")
        cls_mock = MagicMock(return_value=instance)
        monkeypatch.setattr("primer.embedder.huggingface.SentenceTransformer", cls_mock)
        with pytest.raises(AuthenticationError):
            await embedder.embed(model="m1", inputs=[TextPart(text="hi")])

    async def test_oserror_classified_to_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider(models=["m1"])
        embedder = HuggingFaceEmbedder(provider)
        instance = _make_st_mock()
        instance.encode.side_effect = OSError("disk failure")
        cls_mock = MagicMock(return_value=instance)
        monkeypatch.setattr("primer.embedder.huggingface.SentenceTransformer", cls_mock)
        with pytest.raises(NetworkError):
            await embedder.embed(model="m1", inputs=[TextPart(text="hi")])


class TestConcurrency:
    async def test_semaphore_serialises_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider(models=["m1"], max_concurrency=1)
        embedder = HuggingFaceEmbedder(provider)
        in_flight = 0
        peak = 0

        def slow_encode(*args, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            import time
            time.sleep(0.01)
            in_flight -= 1
            return np.array([[0.1]])

        instance = MagicMock()
        instance.encode.side_effect = slow_encode
        cls_mock = MagicMock(return_value=instance)
        monkeypatch.setattr("primer.embedder.huggingface.SentenceTransformer", cls_mock)

        async def consume() -> None:
            await embedder.embed(model="m1", inputs=[TextPart(text="hi")])

        await asyncio.gather(consume(), consume(), consume())
        assert peak == 1


class TestPackageReexport:
    def test_reexported(self) -> None:
        import primer.embedder as e

        assert "HuggingFaceEmbedder" in e.__all__
        assert e.HuggingFaceEmbedder is HuggingFaceEmbedder

    def test_openai_still_reexported(self) -> None:
        import primer.embedder as e

        assert "OpenAIEmbedder" in e.__all__
