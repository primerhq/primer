"""Unit tests for the HuggingFace embedder adapter."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
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
    provider_id: str = "hf-default",
    token: str = "hf-test-token",
    models: list[str] | None = None,
    max_concurrency: int = 4,
) -> EmbeddingProvider:
    return EmbeddingProvider(
        id=provider_id,
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

        # The model refuses a config object of another class than the provider names (review of #645), so the adapter's own check, kept for an
        # object that never went through validation, is reached with ``model_construct``.
        provider = EmbeddingProvider.model_construct(
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
    _clear_model_cache,
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


@pytest.fixture(autouse=True)
def _fresh_model_cache() -> Iterator[None]:
    """The model cache is process-wide: clear it before and after every
    test so a per-test SentenceTransformer patch is never shadowed by a
    cache entry left behind by an earlier test."""
    _clear_model_cache()
    yield
    _clear_model_cache()


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

    def test_prompt_prepended_to_every_text(self) -> None:
        """Asymmetric models need the prompt prepended on the caller side
        (sentence-transformers' built-in prompt support requires model
        registration which we don't rely on)."""
        model = MagicMock()
        model.encode.return_value = np.array([[0.1, 0.2]])
        _encode_sync(
            model, ["alpha", "beta"], "sentence_embedding",
            "Represent this sentence for searching relevant passages: ",
        )
        model.encode.assert_called_once_with(
            [
                "Represent this sentence for searching relevant passages: alpha",
                "Represent this sentence for searching relevant passages: beta",
            ],
            output_value="sentence_embedding",
            convert_to_numpy=True,
            normalize_embeddings=True,
        )


class TestPromptResolution:
    """Model-family → prompt mapping."""

    def test_bge_query_prompt(self) -> None:
        from primer.embedder.huggingface import _resolve_prompts_for_model
        q, d = _resolve_prompts_for_model("BAAI/bge-small-en-v1.5")
        assert q == "Represent this sentence for searching relevant passages: "
        assert d is None

    def test_bge_match_is_case_insensitive(self) -> None:
        from primer.embedder.huggingface import _resolve_prompts_for_model
        q, d = _resolve_prompts_for_model("BAAI/BGE-Large-EN-v1.5")
        assert q is not None
        assert d is None

    def test_e5_has_symmetric_prefixes(self) -> None:
        from primer.embedder.huggingface import _resolve_prompts_for_model
        q, d = _resolve_prompts_for_model("intfloat/multilingual-e5-large")
        assert q == "query: "
        assert d == "passage: "

    def test_nomic_embed_text_has_symmetric_prefixes(self) -> None:
        from primer.embedder.huggingface import _resolve_prompts_for_model
        q, d = _resolve_prompts_for_model("nomic-ai/nomic-embed-text-v1.5")
        assert q == "search_query: "
        assert d == "search_document: "

    def test_unknown_model_returns_none_pair(self) -> None:
        from primer.embedder.huggingface import _resolve_prompts_for_model
        q, d = _resolve_prompts_for_model("sentence-transformers/all-MiniLM-L6-v2")
        assert (q, d) == (None, None)

    def test_select_prompt_routes_by_task_type(self) -> None:
        from primer.embedder.huggingface import _select_prompt
        bge = "BAAI/bge-small-en-v1.5"
        assert _select_prompt(
            task_type="retrieval_query", model_name=bge, raw={},
        ) == "Represent this sentence for searching relevant passages: "
        assert _select_prompt(
            task_type="retrieval_document", model_name=bge, raw={},
        ) is None
        assert _select_prompt(task_type=None, model_name=bge, raw={}) is None

    def test_raw_query_prompt_overrides_family_default(self) -> None:
        from primer.embedder.huggingface import _select_prompt
        result = _select_prompt(
            task_type="retrieval_query",
            model_name="BAAI/bge-small-en-v1.5",
            raw={"query_prompt": "custom: "},
        )
        assert result == "custom: "


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


class TestToken:
    """The token is optional (a public local model needs none): a missing and an empty one both load anonymously."""

    @staticmethod
    def _provider(config: HuggingFaceConfig) -> EmbeddingProvider:
        return EmbeddingProvider(
            id="hf-anon",
            provider=EmbeddingProviderType.HUGGINGFACE,
            models=[EmbeddingModel(name="m1")],
            config=config,
            limits=Limits(max_concurrency=1),
        )

    @pytest.mark.parametrize(
        "config",
        [HuggingFaceConfig(), HuggingFaceConfig(token=None), HuggingFaceConfig(token=SecretStr(""))],
        ids=["omitted", "null", "empty"],
    )
    async def test_no_token_loads_the_model_anonymously(
        self, monkeypatch: pytest.MonkeyPatch, config: HuggingFaceConfig
    ) -> None:
        embedder = HuggingFaceEmbedder(self._provider(config))
        cls_mock, _ = _patched_st(monkeypatch, np.array([[0.1, 0.2, 0.3]]))
        await embedder.embed(model="m1", inputs=[TextPart(text="hi")])
        assert cls_mock.call_args.kwargs["token"] is None

    async def test_a_given_token_is_passed_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        embedder = HuggingFaceEmbedder(self._provider(HuggingFaceConfig(token=SecretStr("hf-given"))))
        cls_mock, _ = _patched_st(monkeypatch, np.array([[0.1, 0.2, 0.3]]))
        await embedder.embed(model="m1", inputs=[TextPart(text="hi")])
        assert cls_mock.call_args.kwargs["token"] == "hf-given"


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


class TestProcessWideModelCache:
    """The model cache is process-wide, keyed by (model name, token).

    Provider rows are per-connection objects, but the loaded
    sentence-transformers model is per (model name, token): the e2e
    lifecycle suite seeds 34 rows with the same model, and the per-row
    cache re-paid the ~90MB load on every bootstrap (PR #835 capacity
    recurrence). Two rows sharing a model + token load it once; different
    models or different tokens do not collide.
    """

    async def test_two_provider_rows_same_model_and_token_load_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cls_mock, instance = _patched_st(monkeypatch, np.array([[0.1]]))
        embedder_a = HuggingFaceEmbedder(
            _make_provider(provider_id="row-a", models=["m1"])
        )
        embedder_b = HuggingFaceEmbedder(
            _make_provider(provider_id="row-b", models=["m1"])
        )
        out_a = await embedder_a.embed(model="m1", inputs=[TextPart(text="a")])
        out_b = await embedder_b.embed(model="m1", inputs=[TextPart(text="b")])
        assert cls_mock.call_count == 1
        assert instance.encode.call_count == 2
        assert out_a.embeddings[0].vector == pytest.approx(
            out_b.embeddings[0].vector
        )

    async def test_different_model_names_do_not_collide(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cls_mock, _ = _patched_st(monkeypatch, np.array([[0.1]]))
        embedder = HuggingFaceEmbedder(_make_provider(models=["m1", "m2"]))
        await embedder.embed(model="m1", inputs=[TextPart(text="a")])
        await embedder.embed(model="m2", inputs=[TextPart(text="b")])
        assert cls_mock.call_count == 2

    async def test_different_tokens_do_not_collide(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cls_mock, _ = _patched_st(monkeypatch, np.array([[0.1]]))
        embedder_a = HuggingFaceEmbedder(
            _make_provider(provider_id="row-a", models=["m1"], token="tok-a")
        )
        embedder_b = HuggingFaceEmbedder(
            _make_provider(provider_id="row-b", models=["m1"], token="tok-b")
        )
        await embedder_a.embed(model="m1", inputs=[TextPart(text="a")])
        await embedder_b.embed(model="m1", inputs=[TextPart(text="b")])
        assert cls_mock.call_count == 2

    async def test_failed_load_is_not_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A load failure propagates raw (embed only classifies encode
        # failures); the key must not be cached, so the retry loads again.
        instance = _make_st_mock(np.array([[0.1]]))
        cls_mock = MagicMock(
            side_effect=[RuntimeError("hub down"), instance]
        )
        monkeypatch.setattr("primer.embedder.huggingface.SentenceTransformer", cls_mock)
        embedder = HuggingFaceEmbedder(_make_provider(models=["m1"]))
        with pytest.raises(RuntimeError, match="hub down"):
            await embedder.embed(model="m1", inputs=[TextPart(text="a")])
        out = await embedder.embed(model="m1", inputs=[TextPart(text="b")])
        assert cls_mock.call_count == 2
        assert out.embeddings[0].vector == pytest.approx([0.1])

    async def test_concurrent_first_loads_of_one_key_dedup_to_a_single_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loaded = threading.Event()

        def slow_load(*args, **kwargs):
            loaded.set()
            time.sleep(0.2)
            return _make_st_mock(np.array([[0.1]]))

        cls_mock = MagicMock(side_effect=slow_load)
        monkeypatch.setattr("primer.embedder.huggingface.SentenceTransformer", cls_mock)
        # Distinct rate-limit keys (distinct provider ids), so the per-row
        # limiter cannot serialise the two first loads for us.
        embedder_a = HuggingFaceEmbedder(
            _make_provider(provider_id="row-a", models=["m1"], max_concurrency=1)
        )
        embedder_b = HuggingFaceEmbedder(
            _make_provider(provider_id="row-b", models=["m1"], max_concurrency=1)
        )
        out_a, out_b = await asyncio.gather(
            embedder_a.embed(model="m1", inputs=[TextPart(text="a")]),
            embedder_b.embed(model="m1", inputs=[TextPart(text="b")]),
        )
        assert cls_mock.call_count == 1
        assert loaded.is_set()
        assert out_a.embeddings[0].vector == pytest.approx(
            out_b.embeddings[0].vector
        )

    async def test_cancelled_first_waiter_does_not_cancel_sibling_waiters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from primer.embedder import huggingface as hf

        def slow_load(*args, **kwargs):
            time.sleep(0.15)
            return _make_st_mock(np.array([[0.1]]))

        cls_mock = MagicMock(side_effect=slow_load)
        monkeypatch.setattr(hf, "SentenceTransformer", cls_mock)
        first = asyncio.create_task(hf._load_shared_model("m1", None))
        await asyncio.sleep(0.02)
        second = asyncio.create_task(hf._load_shared_model("m1", None))
        await asyncio.sleep(0.02)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        result = await second
        assert result is not None
        assert cls_mock.call_count == 1

    async def test_failed_load_with_no_waiter_logs_no_never_retrieved(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import gc
        from primer.embedder import huggingface as hf

        monkeypatch.setattr(
            hf, "SentenceTransformer", MagicMock(side_effect=RuntimeError("hub down"))
        )
        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError, match="hub down"):
                await hf._load_shared_model("m1", None)
            gc.collect()
            await asyncio.sleep(0)
        hits = [
            r.getMessage()
            for r in caplog.records
            if "never retrieved" in r.getMessage()
        ]
        assert hits == []

    async def test_aclose_evicts_unreferenced_keys_and_retains_shared_ones(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from primer.embedder import huggingface as hf

        _patched_st(monkeypatch, np.array([[0.1]]))
        for token in ("t1", "t2", "t3"):
            emb = HuggingFaceEmbedder(
                _make_provider(provider_id="hf-row", models=["m1"], token=token)
            )
            await emb.embed(model="m1", inputs=[TextPart(text="x")])
            await emb.aclose()
        assert len(hf._model_cache) == 0

        # When two rows share a (model, token) key, closing one retains the
        # cached model until the second also closes.
        emb_a = HuggingFaceEmbedder(
            _make_provider(provider_id="row-a", models=["m1"], token="shared")
        )
        emb_b = HuggingFaceEmbedder(
            _make_provider(provider_id="row-b", models=["m1"], token="shared")
        )
        await emb_a.embed(model="m1", inputs=[TextPart(text="a")])
        await emb_b.embed(model="m1", inputs=[TextPart(text="b")])
        assert ("m1", "shared") in hf._model_cache
        await emb_a.aclose()
        await emb_a.aclose()  # idempotent
        assert ("m1", "shared") in hf._model_cache
        await emb_b.aclose()
        assert ("m1", "shared") not in hf._model_cache



class TestPackageReexport:
    def test_reexported(self) -> None:
        import primer.embedder as e

        assert "HuggingFaceEmbedder" in e.__all__
        assert e.HuggingFaceEmbedder is HuggingFaceEmbedder

    def test_openai_still_reexported(self) -> None:
        import primer.embedder as e

        assert "OpenAIEmbedder" in e.__all__
