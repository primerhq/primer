"""Schema tests for the aggregated LLM provider type and config."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.provider import (
    AggregatedLLMConfig,
    AggregatedMember,
    FailoverClasses,
    FailoverPoint,
    LLMProvider,
    LLMProviderType,
    RoutingStrategy,
)


class TestEnumValues:
    def test_provider_type_value_is_stable(self):
        assert LLMProviderType.AGGREGATED.value == "aggregated"

    def test_strategy_values(self):
        assert RoutingStrategy.SEQUENTIAL.value == "sequential"
        assert RoutingStrategy.ROUND_ROBIN.value == "round_robin"

    def test_failover_point_values(self):
        assert FailoverPoint.BEFORE_FIRST_TOKEN.value == "before_first_token"
        assert FailoverPoint.MID_STREAM.value == "mid_stream"

    def test_failover_classes_values(self):
        assert FailoverClasses.TRANSIENT.value == "transient"
        assert FailoverClasses.TRANSIENT_AND_CONFIG.value == "transient_and_config"


class TestAggregatedLLMConfig:
    def test_defaults(self):
        cfg = AggregatedLLMConfig(
            members=[AggregatedMember(provider_id="p1", model_name="m1")],
        )
        assert cfg.strategy == RoutingStrategy.SEQUENTIAL
        assert cfg.failover_point == FailoverPoint.BEFORE_FIRST_TOKEN
        assert cfg.failover_on == FailoverClasses.TRANSIENT_AND_CONFIG

    def test_empty_members_raises(self):
        with pytest.raises(ValidationError):
            AggregatedLLMConfig(members=[])

    def test_dedupes_pairs_preserving_order(self):
        cfg = AggregatedLLMConfig(
            members=[
                AggregatedMember(provider_id="p1", model_name="a"),
                AggregatedMember(provider_id="p1", model_name="b"),
                AggregatedMember(provider_id="p1", model_name="a"),  # dup
                AggregatedMember(provider_id="p2", model_name="a"),
            ],
        )
        assert [(m.provider_id, m.model_name) for m in cfg.members] == [
            ("p1", "a"), ("p1", "b"), ("p2", "a"),
        ]

    def test_same_provider_different_model_is_not_a_dup(self):
        cfg = AggregatedLLMConfig(
            members=[
                AggregatedMember(provider_id="p1", model_name="a"),
                AggregatedMember(provider_id="p1", model_name="b"),
            ],
        )
        assert len(cfg.members) == 2


class TestLLMProviderAggregatedDispatch:
    def _body(self, **overrides):
        body = {
            "id": "agg-1",
            "provider": "aggregated",
            "config": {
                "members": [{"provider_id": "p1", "model_name": "m1"}],
                "strategy": "round_robin",
            },
            "models": [{"name": "virtual-1", "context_length": 200000}],
            "limits": {"max_concurrency": 4},
        }
        body.update(overrides)
        return body

    def test_coerce_selects_aggregated_config(self):
        row = LLMProvider.model_validate(self._body())
        assert isinstance(row.config, AggregatedLLMConfig)
        assert row.config.strategy == RoutingStrategy.ROUND_ROBIN
        assert row.config.members[0].provider_id == "p1"

    def test_models_required_min_length_one(self):
        with pytest.raises(ValidationError):
            LLMProvider.model_validate(self._body(models=[]))
