"""Unit tests for the OpenAI embedder adapter."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import openai
import pytest
from pydantic import HttpUrl, SecretStr

from primer.embedder.openai import (
    OpenAIEmbedder,
    _FlavorPolicy,
    _POLICY_BY_FLAVOR,
    _inputs_to_openai_list,
    _part_to_openai_input,
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
    EmbeddingPart,
    EmbeddingUsage,
    ExtendedEmbeddingConfig,
    ExtendedEmbeddingPart,
    TextPart,
    TokensPart,
)
from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    ConfigError,
    ModelNotFoundError,
    ProviderError,
    RateLimitError,
    UnsupportedContentError,
)
from primer.model.provider import (
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingProviderType,
    Limits,
    OpenAIConfig,
    OpenAIEmbeddingFlavor,
)


# ------------------------------------------------------------------------- #
# Test fixtures                                                              #
# ------------------------------------------------------------------------- #


def _make_provider(
    *,
    flavor: OpenAIEmbeddingFlavor = OpenAIEmbeddingFlavor.OPENAI,
    api_key: str = "sk-test",
    models: list[str] | None = None,
    max_concurrency: int = 4,
) -> EmbeddingProvider:
    return EmbeddingProvider(
        id="openai-default",
        provider=EmbeddingProviderType.OPENAI,
        models=[
            EmbeddingModel(name=name)
            for name in (models or ["text-embedding-3-small"])
        ],
        config=OpenAIConfig(
            url=HttpUrl("https://api.openai.com/v1/"),
            api_key=SecretStr(api_key),
            flavor=flavor,
        ),
        limits=Limits(max_concurrency=max_concurrency),
    )


# ------------------------------------------------------------------------- #
# TestFlavorPolicy                                                           #
# ------------------------------------------------------------------------- #


class TestFlavorPolicy:
    def test_openai_policy_requires_api_key(self) -> None:
        policy = _POLICY_BY_FLAVOR[OpenAIEmbeddingFlavor.OPENAI]
        assert policy.require_api_key is True

    def test_lmstudio_policy_does_not_require_api_key(self) -> None:
        policy = _POLICY_BY_FLAVOR[OpenAIEmbeddingFlavor.LMSTUDIO]
        assert policy.require_api_key is False

    def test_other_policy_requires_api_key(self) -> None:
        policy = _POLICY_BY_FLAVOR[OpenAIEmbeddingFlavor.OTHER]
        assert policy.require_api_key is True

    def test_policy_dataclass_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        policy = _POLICY_BY_FLAVOR[OpenAIEmbeddingFlavor.OPENAI]
        with pytest.raises(FrozenInstanceError):
            policy.require_api_key = False  # type: ignore[misc]


# ------------------------------------------------------------------------- #
# TestConstructor                                                            #
# ------------------------------------------------------------------------- #


class TestConstructor:
    def test_accepts_valid_openai_config(self) -> None:
        provider = _make_provider(flavor=OpenAIEmbeddingFlavor.OPENAI)
        embedder = OpenAIEmbedder(provider)
        assert embedder._policy is _POLICY_BY_FLAVOR[OpenAIEmbeddingFlavor.OPENAI]
        assert embedder._client is None  # lazy

    def test_accepts_lmstudio_with_empty_key(self) -> None:
        provider = _make_provider(
            flavor=OpenAIEmbeddingFlavor.LMSTUDIO, api_key=""
        )
        embedder = OpenAIEmbedder(provider)
        assert embedder._policy.require_api_key is False

    def test_accepts_lmstudio_with_non_empty_key_for_proxied_auth(self) -> None:
        # Reverse-proxy use case: LM Studio behind a proxy that enforces
        # its own auth. The flavor doesn't require a key but if one is
        # supplied it must be passed through (verified in TestEmbed).
        provider = _make_provider(
            flavor=OpenAIEmbeddingFlavor.LMSTUDIO, api_key="proxy-secret"
        )
        embedder = OpenAIEmbedder(provider)
        assert embedder._config.api_key.get_secret_value() == "proxy-secret"

    def test_rejects_empty_api_key_for_openai_flavor(self) -> None:
        provider = _make_provider(flavor=OpenAIEmbeddingFlavor.OPENAI, api_key="")
        with pytest.raises(ConfigError, match="api_key is required"):
            OpenAIEmbedder(provider)

    def test_rejects_wrong_provider_type(self) -> None:
        provider = _make_provider()
        # Tamper with the validated provider for the test.
        object.__setattr__(provider, "provider", "huggingface")  # type: ignore[arg-type]
        with pytest.raises(ConfigError, match="OPENAI"):
            OpenAIEmbedder(provider)

    def test_initialises_rate_limiter_and_max_concurrency(self) -> None:
        provider = _make_provider(max_concurrency=3)
        embedder = OpenAIEmbedder(provider)
        assert embedder._rate_limiter is not None
        assert embedder._max_concurrency == 3
        assert embedder._rate_limit_key == "embedder:openai-default"

    def test_logs_init_with_structured_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="primer.embedder.openai")
        provider = _make_provider(
            models=["text-embedding-3-small", "text-embedding-3-large"],
            max_concurrency=2,
        )
        OpenAIEmbedder(provider)
        records = [
            r for r in caplog.records if "OpenAI embedder initialized" in r.message
        ]
        assert len(records) == 1
        record = records[0]
        assert record.provider_id == "openai-default"  # type: ignore[attr-defined]
        assert record.flavor == "openai"  # type: ignore[attr-defined]
        assert record.models == [  # type: ignore[attr-defined]
            "text-embedding-3-small",
            "text-embedding-3-large",
        ]
        assert record.max_concurrency == 2  # type: ignore[attr-defined]


# ------------------------------------------------------------------------- #
# TestListModels                                                             #
# ------------------------------------------------------------------------- #


class TestListModels:
    async def test_returns_configured_model_names(self) -> None:
        provider = _make_provider(
            models=["text-embedding-3-small", "text-embedding-3-large"]
        )
        embedder = OpenAIEmbedder(provider)
        models = list(await embedder.list_models())
        assert models == ["text-embedding-3-small", "text-embedding-3-large"]

    async def test_does_not_call_upstream(self) -> None:
        provider = _make_provider()
        embedder = OpenAIEmbedder(provider)
        with patch.object(OpenAIEmbedder, "_get_client") as mock_get_client:
            await embedder.list_models()
            mock_get_client.assert_not_called()


# ------------------------------------------------------------------------- #
# TestPartToOpenaiInput                                                      #
# ------------------------------------------------------------------------- #


class TestPartToOpenaiInput:
    def test_text_part_maps_to_string(self) -> None:
        assert _part_to_openai_input(TextPart(text="hello")) == "hello"

    def test_tokens_part_maps_to_int_list(self) -> None:
        part = ExtendedEmbeddingPart(
            extended=TokensPart(tokens=[1, 2, 3], tokenizer="cl100k_base")
        )
        assert _part_to_openai_input(part) == [1, 2, 3]

    def test_tokens_part_without_tokenizer_still_maps(self) -> None:
        part = ExtendedEmbeddingPart(extended=TokensPart(tokens=[42]))
        assert _part_to_openai_input(part) == [42]

    def test_image_part_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="text-only"):
            _part_to_openai_input(ImagePart(url="https://example.com/img.png"))

    def test_audio_part_raises(self) -> None:
        part = ExtendedEmbeddingPart(extended=AudioPart(url="https://example.com/a.mp3"))
        with pytest.raises(UnsupportedContentError, match="audio"):
            _part_to_openai_input(part)

    def test_video_part_raises(self) -> None:
        part = ExtendedEmbeddingPart(extended=VideoPart(url="https://example.com/v.mp4"))
        with pytest.raises(UnsupportedContentError, match="video"):
            _part_to_openai_input(part)

    def test_document_part_raises(self) -> None:
        part = ExtendedEmbeddingPart(
            extended=DocumentPart(url="https://example.com/d.pdf")
        )
        with pytest.raises(UnsupportedContentError, match="document"):
            _part_to_openai_input(part)


# ------------------------------------------------------------------------- #
# TestInputsToOpenaiList                                                     #
# ------------------------------------------------------------------------- #


class TestInputsToOpenaiList:
    def test_empty_list(self) -> None:
        assert _inputs_to_openai_list([]) == []

    def test_pure_text_batch(self) -> None:
        inputs: list[EmbeddingPart] = [
            TextPart(text="one"),
            TextPart(text="two"),
            TextPart(text="three"),
        ]
        assert _inputs_to_openai_list(inputs) == ["one", "two", "three"]

    def test_pure_tokens_batch(self) -> None:
        inputs: list[EmbeddingPart] = [
            ExtendedEmbeddingPart(extended=TokensPart(tokens=[1, 2])),
            ExtendedEmbeddingPart(extended=TokensPart(tokens=[3, 4, 5])),
        ]
        assert _inputs_to_openai_list(inputs) == [[1, 2], [3, 4, 5]]

    def test_mixed_text_and_tokens_preserves_order(self) -> None:
        inputs: list[EmbeddingPart] = [
            TextPart(text="alpha"),
            ExtendedEmbeddingPart(extended=TokensPart(tokens=[10])),
            TextPart(text="beta"),
        ]
        assert _inputs_to_openai_list(inputs) == ["alpha", [10], "beta"]

    def test_first_unsupported_part_raises_with_index_context(self) -> None:
        inputs: list[EmbeddingPart] = [
            TextPart(text="ok"),
            ImagePart(url="https://example.com/img.png"),
        ]
        with pytest.raises(UnsupportedContentError, match="text-only"):
            _inputs_to_openai_list(inputs)


# ------------------------------------------------------------------------- #
# TestTranslateResponse                                                      #
# ------------------------------------------------------------------------- #


class TestTranslateResponse:
    def test_single_embedding_no_usage(self) -> None:
        resp = NS(
            model="text-embedding-3-small",
            data=[NS(index=0, embedding=[0.1, 0.2, 0.3])],
            usage=None,
        )
        out = _translate_response(resp)
        assert isinstance(out, EmbedResponse)
        assert out.model == "text-embedding-3-small"
        assert out.usage is None
        assert len(out.embeddings) == 1
        emb = out.embeddings[0]
        assert isinstance(emb, Embedding)
        assert emb.index == 0
        assert emb.vector == [0.1, 0.2, 0.3]
        assert emb.extended is None

    def test_batch_preserves_index_order(self) -> None:
        resp = NS(
            model="text-embedding-3-small",
            data=[
                NS(index=0, embedding=[0.1]),
                NS(index=1, embedding=[0.2]),
                NS(index=2, embedding=[0.3]),
            ],
            usage=None,
        )
        out = _translate_response(resp)
        assert [e.index for e in out.embeddings] == [0, 1, 2]
        assert [e.vector for e in out.embeddings] == [[0.1], [0.2], [0.3]]

    def test_with_usage(self) -> None:
        resp = NS(
            model="text-embedding-3-small",
            data=[NS(index=0, embedding=[0.1])],
            usage=NS(prompt_tokens=42, total_tokens=42),
        )
        out = _translate_response(resp)
        assert isinstance(out.usage, EmbeddingUsage)
        assert out.usage.input_tokens == 42
        assert out.usage.input_characters is None

    def test_usage_without_prompt_tokens_field(self) -> None:
        # Defensive: SDK could rename the field. getattr fallback returns None.
        resp = NS(
            model="text-embedding-3-small",
            data=[NS(index=0, embedding=[0.1])],
            usage=NS(total_tokens=5),  # no prompt_tokens attr
        )
        out = _translate_response(resp)
        assert isinstance(out.usage, EmbeddingUsage)
        assert out.usage.input_tokens is None

    def test_vector_is_materialised_as_list(self) -> None:
        # Some SDK versions return numpy-like sequences. Ensure conversion to list.
        resp = NS(
            model="text-embedding-3-small",
            data=[NS(index=0, embedding=(0.1, 0.2))],  # tuple, not list
            usage=None,
        )
        out = _translate_response(resp)
        assert out.embeddings[0].vector == [0.1, 0.2]
        assert isinstance(out.embeddings[0].vector, list)


# ------------------------------------------------------------------------- #
# Embed orchestrator helpers                                                 #
# ------------------------------------------------------------------------- #


def _make_openai_error(cls: type, *, status_code: int = 400, code: str | None = None):
    """Build an openai SDK exception with minimal init plumbing (test bypass)."""
    exc = cls.__new__(cls)
    exc.status_code = status_code
    exc.code = code
    exc.message = f"test {cls.__name__}"
    Exception.__init__(exc, exc.message)
    return exc


def _patched_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch the AsyncOpenAI symbol in the embedder module to a MagicMock."""
    mock_instance = MagicMock()
    mock_instance.embeddings = MagicMock()
    mock_instance.embeddings.create = AsyncMock()
    cls_mock = MagicMock(return_value=mock_instance)
    monkeypatch.setattr("primer.embedder.openai.AsyncOpenAI", cls_mock)
    return mock_instance


def _ok_response(
    *, model: str = "text-embedding-3-small", count: int = 1, dim: int = 1
):
    """Build a mock OpenAI embeddings response object."""
    from types import SimpleNamespace as NS

    return NS(
        model=model,
        data=[NS(index=i, embedding=[0.1 * (i + 1)] * dim) for i in range(count)],
        usage=NS(prompt_tokens=count, total_tokens=count),
    )


class TestEmbed:
    async def test_unknown_model_raises_model_not_found(self) -> None:
        provider = _make_provider(models=["text-embedding-3-small"])
        embedder = OpenAIEmbedder(provider)
        with pytest.raises(ModelNotFoundError, match="not-a-real-model"):
            await embedder.embed(
                model="not-a-real-model",
                inputs=[TextPart(text="hi")],
            )

    async def test_basic_embed_returns_translated_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        embedder = OpenAIEmbedder(provider)
        client = _patched_client(monkeypatch)
        client.embeddings.create.return_value = _ok_response(count=2, dim=3)

        out = await embedder.embed(
            model="text-embedding-3-small",
            inputs=[TextPart(text="alpha"), TextPart(text="beta")],
        )

        assert out.model == "text-embedding-3-small"
        assert len(out.embeddings) == 2
        assert out.embeddings[0].index == 0
        assert out.embeddings[0].vector == [0.1, 0.1, 0.1]
        assert out.embeddings[1].index == 1
        assert out.embeddings[1].vector == [0.2, 0.2, 0.2]
        assert out.usage is not None
        assert out.usage.input_tokens == 2

    async def test_request_payload_minimal_when_no_optionals(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        embedder = OpenAIEmbedder(provider)
        client = _patched_client(monkeypatch)
        client.embeddings.create.return_value = _ok_response()
        await embedder.embed(
            model="text-embedding-3-small",
            inputs=[TextPart(text="hi")],
        )
        kwargs = client.embeddings.create.call_args.kwargs
        assert kwargs == {
            "model": "text-embedding-3-small",
            "input": ["hi"],
        }

    async def test_request_payload_includes_dimensions_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        embedder = OpenAIEmbedder(provider)
        client = _patched_client(monkeypatch)
        client.embeddings.create.return_value = _ok_response()
        await embedder.embed(
            model="text-embedding-3-small",
            inputs=[TextPart(text="hi")],
            output_dimensions=512,
        )
        kwargs = client.embeddings.create.call_args.kwargs
        assert kwargs["dimensions"] == 512

    async def test_request_payload_includes_user_when_config_user_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        embedder = OpenAIEmbedder(provider)
        client = _patched_client(monkeypatch)
        client.embeddings.create.return_value = _ok_response()
        await embedder.embed(
            model="text-embedding-3-small",
            inputs=[TextPart(text="hi")],
            config=ExtendedEmbeddingConfig(user="user-123"),
        )
        kwargs = client.embeddings.create.call_args.kwargs
        assert kwargs["user"] == "user-123"

    async def test_request_payload_omits_dimensions_when_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        embedder = OpenAIEmbedder(provider)
        client = _patched_client(monkeypatch)
        client.embeddings.create.return_value = _ok_response()
        await embedder.embed(
            model="text-embedding-3-small",
            inputs=[TextPart(text="hi")],
            output_dimensions=None,
        )
        kwargs = client.embeddings.create.call_args.kwargs
        assert "dimensions" not in kwargs

    async def test_request_payload_omits_user_when_config_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        embedder = OpenAIEmbedder(provider)
        client = _patched_client(monkeypatch)
        client.embeddings.create.return_value = _ok_response()
        await embedder.embed(
            model="text-embedding-3-small",
            inputs=[TextPart(text="hi")],
        )
        kwargs = client.embeddings.create.call_args.kwargs
        assert "user" not in kwargs

    async def test_unsupported_input_raises_before_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        embedder = OpenAIEmbedder(provider)
        client = _patched_client(monkeypatch)
        with pytest.raises(UnsupportedContentError, match="text-only"):
            await embedder.embed(
                model="text-embedding-3-small",
                inputs=[ImagePart(url="https://example.com/img.png")],
            )
        client.embeddings.create.assert_not_called()


class TestExceptionWrapping:
    async def test_authentication_exception_classified_and_re_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        embedder = OpenAIEmbedder(provider)
        client = _patched_client(monkeypatch)
        client.embeddings.create.side_effect = _make_openai_error(
            openai.AuthenticationError, status_code=401
        )
        with pytest.raises(AuthenticationError):
            await embedder.embed(
                model="text-embedding-3-small",
                inputs=[TextPart(text="hi")],
            )

    async def test_bad_request_exception_classified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        embedder = OpenAIEmbedder(provider)
        client = _patched_client(monkeypatch)
        client.embeddings.create.side_effect = _make_openai_error(
            openai.BadRequestError, status_code=400, code="invalid_dimensions"
        )
        with pytest.raises(BadRequestError) as info:
            await embedder.embed(
                model="text-embedding-3-small",
                inputs=[TextPart(text="hi")],
                output_dimensions=99999,
            )
        assert info.value.code == "invalid_dimensions"

    async def test_rate_limit_exception_classified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        embedder = OpenAIEmbedder(provider)
        client = _patched_client(monkeypatch)
        client.embeddings.create.side_effect = _make_openai_error(
            openai.RateLimitError, status_code=429
        )
        with pytest.raises(RateLimitError):
            await embedder.embed(
                model="text-embedding-3-small",
                inputs=[TextPart(text="hi")],
            )


class TestConcurrency:
    async def test_semaphore_serialises_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider(max_concurrency=1)
        embedder = OpenAIEmbedder(provider)
        client = _patched_client(monkeypatch)

        in_flight = 0
        peak = 0

        async def slow_create(**_kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return _ok_response()

        client.embeddings.create.side_effect = slow_create

        async def consume() -> None:
            await embedder.embed(
                model="text-embedding-3-small",
                inputs=[TextPart(text="hi")],
            )

        await asyncio.gather(consume(), consume(), consume())
        assert peak == 1


class TestExtendedConfig:
    async def test_user_is_forwarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        embedder = OpenAIEmbedder(provider)
        client = _patched_client(monkeypatch)
        client.embeddings.create.return_value = _ok_response()
        await embedder.embed(
            model="text-embedding-3-small",
            inputs=[TextPart(text="hi")],
            config=ExtendedEmbeddingConfig(user="abc"),
        )
        assert client.embeddings.create.call_args.kwargs["user"] == "abc"

    @pytest.mark.parametrize(
        "config",
        [
            ExtendedEmbeddingConfig(task_type="classification"),
            ExtendedEmbeddingConfig(title="My Doc"),
            ExtendedEmbeddingConfig(auto_truncate=True),
            ExtendedEmbeddingConfig(document_ocr=True),
            ExtendedEmbeddingConfig(audio_track_extraction=True),
            ExtendedEmbeddingConfig(raw={"some": "knob"}),
        ],
    )
    async def test_other_knobs_silently_ignored(
        self,
        monkeypatch: pytest.MonkeyPatch,
        config: ExtendedEmbeddingConfig,
    ) -> None:
        provider = _make_provider()
        embedder = OpenAIEmbedder(provider)
        client = _patched_client(monkeypatch)
        client.embeddings.create.return_value = _ok_response()
        await embedder.embed(
            model="text-embedding-3-small",
            inputs=[TextPart(text="hi")],
            config=config,
        )
        kwargs = client.embeddings.create.call_args.kwargs
        # Only model + input; nothing else slipped through
        assert set(kwargs.keys()) == {"model", "input"}


class TestPackageReexport:
    def test_openai_embedder_reexported_from_package(self) -> None:
        import primer.embedder as embedder_pkg

        assert "OpenAIEmbedder" in embedder_pkg.__all__
        assert embedder_pkg.OpenAIEmbedder is OpenAIEmbedder

