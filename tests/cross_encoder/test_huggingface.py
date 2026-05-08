"""Unit tests for the HuggingFace cross-encoder adapter."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from matrix.cross_encoder.huggingface import HuggingFaceCrossEncoder
from matrix.model.except_ import BadRequestError, ConfigError, ProviderError
from matrix.model.provider import (
    CrossEncoderModel,
    CrossEncoderProvider,
    CrossEncoderProviderType,
    HuggingFaceCrossEncoderConfig,
    Limits,
)


def _make_provider(
    *,
    token: str | None = None,
    models: list[str] | None = None,
    max_concurrency: int = 4,
    max_pair_length: int | None = None,
) -> CrossEncoderProvider:
    return CrossEncoderProvider(
        id="hf-ce",
        provider=CrossEncoderProviderType.HUGGINGFACE,
        models=[
            CrossEncoderModel(name=name, max_pair_length=max_pair_length)
            for name in (models or ["BAAI/bge-reranker-v2-m3"])
        ],
        config=HuggingFaceCrossEncoderConfig(
            token=SecretStr(token) if token else None,
        ),
        limits=Limits(max_concurrency=max_concurrency),
    )


class TestConstructor:
    def test_accepts_valid_config(self) -> None:
        provider = _make_provider()
        adapter = HuggingFaceCrossEncoder(provider)
        assert adapter._models == {}

    def test_rejects_wrong_provider_type(self) -> None:
        provider = _make_provider()
        # Force a non-HUGGINGFACE provider value to assert the guard.
        object.__setattr__(provider, "provider", "cohere")  # type: ignore[arg-type]
        with pytest.raises(ConfigError, match="HUGGINGFACE"):
            HuggingFaceCrossEncoder(provider)

    def test_initialises_semaphore_to_max_concurrency(self) -> None:
        provider = _make_provider(max_concurrency=3)
        adapter = HuggingFaceCrossEncoder(provider)
        assert isinstance(adapter._semaphore, asyncio.Semaphore)
        assert adapter._semaphore._value == 3  # type: ignore[attr-defined]

    def test_logs_init_with_structured_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="matrix.cross_encoder.huggingface")
        provider = _make_provider(models=["a", "b"], max_concurrency=2)
        HuggingFaceCrossEncoder(provider)
        records = [
            r for r in caplog.records
            if "HuggingFace cross-encoder initialized" in r.message
        ]
        assert len(records) == 1
        record = records[0]
        assert record.provider_id == "hf-ce"  # type: ignore[attr-defined]
        assert record.models == ["a", "b"]  # type: ignore[attr-defined]
        assert record.max_concurrency == 2  # type: ignore[attr-defined]


class TestListModels:
    @pytest.mark.asyncio
    async def test_returns_registered_model_names(self) -> None:
        provider = _make_provider(models=["m1", "m2", "m3"])
        adapter = HuggingFaceCrossEncoder(provider)
        names = list(await adapter.list_models())
        assert names == ["m1", "m2", "m3"]


class TestScore:
    @pytest.mark.asyncio
    async def test_empty_documents_short_circuits(self) -> None:
        provider = _make_provider()
        adapter = HuggingFaceCrossEncoder(provider)
        # No model is loaded (zero docs ⇒ zero pairs ⇒ no predict call).
        with patch(
            "matrix.cross_encoder.huggingface.STCrossEncoder"
        ) as mock_st:
            scores = await adapter.score(
                model="BAAI/bge-reranker-v2-m3",
                query="q",
                documents=[],
            )
        assert scores == []
        mock_st.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_batch_size_raises(self) -> None:
        provider = _make_provider()
        adapter = HuggingFaceCrossEncoder(provider)
        with pytest.raises(BadRequestError, match="batch_size"):
            await adapter.score(
                model="BAAI/bge-reranker-v2-m3",
                query="q",
                documents=["a"],
                batch_size=0,
            )

    @pytest.mark.asyncio
    async def test_unknown_model_raises_config_error(self) -> None:
        provider = _make_provider(models=["only-known"])
        adapter = HuggingFaceCrossEncoder(provider)
        with pytest.raises(ConfigError, match="not registered"):
            await adapter.score(
                model="never-heard-of",
                query="q",
                documents=["d"],
            )

    @pytest.mark.asyncio
    async def test_lazy_loads_then_caches_model(self) -> None:
        """First call loads; second call reuses the cached instance."""
        provider = _make_provider(models=["m1"])
        adapter = HuggingFaceCrossEncoder(provider)

        fake_model = MagicMock()
        fake_model.predict.return_value = [0.7, 0.3]

        with patch(
            "matrix.cross_encoder.huggingface.STCrossEncoder",
            return_value=fake_model,
        ) as mock_st:
            await adapter.score(model="m1", query="q", documents=["a", "b"])
            await adapter.score(model="m1", query="q", documents=["c", "d"])

        # Constructor was called exactly once.
        assert mock_st.call_count == 1
        # predict was called twice (once per score call).
        assert fake_model.predict.call_count == 2

    @pytest.mark.asyncio
    async def test_returns_one_score_per_doc_in_input_order(self) -> None:
        provider = _make_provider(models=["m1"])
        adapter = HuggingFaceCrossEncoder(provider)

        fake_model = MagicMock()
        fake_model.predict.return_value = [0.9, 0.1, 0.5]

        with patch(
            "matrix.cross_encoder.huggingface.STCrossEncoder",
            return_value=fake_model,
        ):
            scores = await adapter.score(
                model="m1",
                query="capital of france",
                documents=["paris", "berlin", "lyon"],
                batch_size=8,
            )

        assert scores == [0.9, 0.1, 0.5]
        # Pairs were built from (query, doc) and batch_size was forwarded.
        call = fake_model.predict.call_args
        assert call.args[0] == [
            ("capital of france", "paris"),
            ("capital of france", "berlin"),
            ("capital of france", "lyon"),
        ]
        assert call.kwargs["batch_size"] == 8

    @pytest.mark.asyncio
    async def test_passes_max_pair_length_to_constructor(self) -> None:
        """When provider catalogue sets max_pair_length, we forward it as max_length."""
        provider = _make_provider(models=["m1"], max_pair_length=512)
        adapter = HuggingFaceCrossEncoder(provider)

        fake_model = MagicMock()
        fake_model.predict.return_value = [0.5]

        with patch(
            "matrix.cross_encoder.huggingface.STCrossEncoder",
            return_value=fake_model,
        ) as mock_st:
            await adapter.score(model="m1", query="q", documents=["d"])

        # Constructor saw max_length=512.
        ctor_call = mock_st.call_args
        assert ctor_call.kwargs.get("max_length") == 512

    @pytest.mark.asyncio
    async def test_predict_size_mismatch_raises(self) -> None:
        provider = _make_provider(models=["m1"])
        adapter = HuggingFaceCrossEncoder(provider)

        # Predictor returns the WRONG number of scores (defensive guard).
        fake_model = MagicMock()
        fake_model.predict.return_value = [0.1]  # only one for two docs

        with patch(
            "matrix.cross_encoder.huggingface.STCrossEncoder",
            return_value=fake_model,
        ):
            with pytest.raises(ProviderError, match="returned 1 scores for 2"):
                await adapter.score(
                    model="m1", query="q", documents=["a", "b"]
                )
