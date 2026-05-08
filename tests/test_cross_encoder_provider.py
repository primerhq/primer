"""Unit tests for the CrossEncoderProvider config types in matrix/model/provider.py.

Covers:

* :class:`HuggingFaceCrossEncoderConfig` — backend connection config
  (token is optional; required only for gated repos).
* :class:`CrossEncoderModel` — name + optional max_pair_length.
* :class:`CrossEncoderProvider` — provider/models/config/limits round-trip.
* :class:`CrossEncoderProviderType` enum.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from matrix.model.provider import (
    CrossEncoderModel,
    CrossEncoderProvider,
    CrossEncoderProviderType,
    HuggingFaceCrossEncoderConfig,
    Limits,
)


class TestHuggingFaceCrossEncoderConfig:
    def test_token_optional(self) -> None:
        cfg = HuggingFaceCrossEncoderConfig()
        assert cfg.token is None

    def test_token_can_be_set(self) -> None:
        cfg = HuggingFaceCrossEncoderConfig(token=SecretStr("hf_xyz"))
        assert cfg.token is not None
        assert cfg.token.get_secret_value() == "hf_xyz"


class TestCrossEncoderModel:
    def test_minimal(self) -> None:
        m = CrossEncoderModel(name="BAAI/bge-reranker-v2-m3")
        assert m.name == "BAAI/bge-reranker-v2-m3"
        assert m.max_pair_length is None

    def test_with_max_pair_length(self) -> None:
        m = CrossEncoderModel(
            name="cross-encoder/ms-marco-MiniLM-L-6-v2",
            max_pair_length=512,
        )
        assert m.max_pair_length == 512

    def test_max_pair_length_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            CrossEncoderModel(name="m", max_pair_length=0)
        with pytest.raises(ValidationError):
            CrossEncoderModel(name="m", max_pair_length=-1)

    def test_name_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            CrossEncoderModel(name="")


class TestCrossEncoderProvider:
    def test_construction(self) -> None:
        provider = CrossEncoderProvider(
            id="ce-default",
            provider=CrossEncoderProviderType.HUGGINGFACE,
            models=[
                CrossEncoderModel(name="BAAI/bge-reranker-v2-m3"),
            ],
            config=HuggingFaceCrossEncoderConfig(),
            limits=Limits(max_concurrency=4),
        )
        assert provider.id == "ce-default"
        assert provider.provider == CrossEncoderProviderType.HUGGINGFACE
        assert provider.config.token is None
        assert provider.limits.max_concurrency == 4

    def test_models_required_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            CrossEncoderProvider(
                id="x",
                provider=CrossEncoderProviderType.HUGGINGFACE,
                models=[],
                config=HuggingFaceCrossEncoderConfig(),
                limits=Limits(max_concurrency=1),
            )

    def test_round_trip_through_model_dump(self) -> None:
        original = CrossEncoderProvider(
            id="ce-1",
            provider=CrossEncoderProviderType.HUGGINGFACE,
            models=[
                CrossEncoderModel(
                    name="BAAI/bge-reranker-v2-m3",
                    max_pair_length=1024,
                ),
            ],
            config=HuggingFaceCrossEncoderConfig(token=SecretStr("hf-tok")),
            limits=Limits(max_concurrency=2),
        )
        data = original.model_dump()
        rehydrated = CrossEncoderProvider.model_validate(data)
        assert rehydrated.id == original.id
        assert rehydrated.provider == original.provider
        assert rehydrated.models[0].name == "BAAI/bge-reranker-v2-m3"
        assert rehydrated.models[0].max_pair_length == 1024
        assert rehydrated.limits.max_concurrency == 2


class TestCrossEncoderProviderType:
    def test_huggingface_value_is_stable(self) -> None:
        # The string value is serialized into config files; protect it.
        assert CrossEncoderProviderType.HUGGINGFACE.value == "huggingface"
