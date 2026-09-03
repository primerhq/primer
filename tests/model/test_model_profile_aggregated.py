"""Schema tests for ModelProfile's kind="aggregated" shape.

The aggregation concept used to be its own LLMProvider type
(``LLMProviderType.AGGREGATED``, ``AggregatedLLMConfig``); both are gone
(see ``tests/model/test_provider_aggregated.py``'s git history). It is
now an additive shape on ModelProfile itself -- see this module's
counterpart in ``primer/model/model_profile.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.model_profile import (
    FailoverClasses,
    FailoverPoint,
    ModelProfile,
    RoutingStrategy,
)


class TestEnumValues:
    """Same values as before the move -- only the import path changed."""

    def test_strategy_values(self):
        assert RoutingStrategy.SEQUENTIAL.value == "sequential"
        assert RoutingStrategy.ROUND_ROBIN.value == "round_robin"

    def test_failover_point_values(self):
        assert FailoverPoint.BEFORE_FIRST_TOKEN.value == "before_first_token"
        assert FailoverPoint.MID_STREAM.value == "mid_stream"

    def test_failover_classes_values(self):
        assert FailoverClasses.TRANSIENT.value == "transient"
        assert FailoverClasses.TRANSIENT_AND_CONFIG.value == "transient_and_config"


def _single(**overrides):
    body = {
        "id": "leaf-1",
        "description": "a leaf profile",
        "provider_id": "p1",
        "model_name": "m1",
        "context_length": 8192,
    }
    body.update(overrides)
    return body


def _aggregated(**overrides):
    body = {
        "id": "agg-1",
        "description": "an aggregated profile",
        "kind": "aggregated",
        "members": ["leaf-1", "leaf-2"],
    }
    body.update(overrides)
    return body


class TestKindDefaultsToSingle:
    def test_kind_defaults_to_single(self):
        row = ModelProfile.model_validate(_single())
        assert row.kind == "single"

    def test_a_pre_migration_row_with_no_kind_field_still_validates(self):
        """Every row written before this shape existed has no "kind" key
        at all on disk -- it must read back exactly as "single", not
        fail validation. This is what lets resolve_model/resolve_llm read
        old rows through the live model without a migration having run
        yet (see m007's own docstring on why ModelProfile itself needs
        no shadow class)."""
        row = ModelProfile.model_validate({
            "id": "old-row", "description": "an old row",
            "provider_id": "p1", "model_name": "m1", "context_length": 8192,
        })
        assert row.kind == "single"
        assert row.members is None


class TestSingleShapeValidation:
    def test_valid_single_profile(self):
        row = ModelProfile.model_validate(_single())
        assert row.provider_id == "p1"
        assert row.model_name == "m1"
        assert row.context_length == 8192
        assert row.members is None

    @pytest.mark.parametrize("missing", ["provider_id", "model_name", "context_length"])
    def test_missing_required_field_raises(self, missing):
        body = _single()
        body[missing] = None
        with pytest.raises(ValidationError, match="kind='single' requires"):
            ModelProfile.model_validate(body)

    def test_single_with_members_set_raises(self):
        body = _single(members=["leaf-2"])
        with pytest.raises(ValidationError, match="kind='single' must not set members"):
            ModelProfile.model_validate(body)


class TestAggregatedShapeValidation:
    def test_valid_aggregated_profile(self):
        row = ModelProfile.model_validate(_aggregated())
        assert row.kind == "aggregated"
        assert row.members == ["leaf-1", "leaf-2"]
        assert row.provider_id is None
        assert row.model_name is None
        assert row.context_length is None

    def test_default_routing_policy(self):
        row = ModelProfile.model_validate(_aggregated())
        assert row.strategy == RoutingStrategy.SEQUENTIAL
        assert row.failover_point == FailoverPoint.BEFORE_FIRST_TOKEN
        assert row.failover_on == FailoverClasses.TRANSIENT_AND_CONFIG

    def test_routing_policy_is_settable(self):
        row = ModelProfile.model_validate(_aggregated(
            strategy="round_robin",
            failover_point="mid_stream",
            failover_on="transient",
        ))
        assert row.strategy == RoutingStrategy.ROUND_ROBIN
        assert row.failover_point == FailoverPoint.MID_STREAM
        assert row.failover_on == FailoverClasses.TRANSIENT

    @pytest.mark.parametrize("field,value", [
        ("provider_id", "p1"), ("model_name", "m1"), ("context_length", 8192),
    ])
    def test_setting_single_only_field_raises(self, field, value):
        body = _aggregated()
        body[field] = value
        with pytest.raises(
            ValidationError, match="kind='aggregated' must not set"
        ):
            ModelProfile.model_validate(body)

    def test_this_model_does_not_itself_enforce_member_count_or_content(self):
        """members>=2 / existence / kind=single-per-member / no self-
        reference / no duplicates are CRUD-time checks on the
        model_profiles router (they need row lookups a bare model can't
        do) -- see tests/api/test_model_profiles_aggregation.py. The bare
        model only enforces the required-fields-by-kind shape, so a
        single-member or self-referential list validates fine here."""
        row = ModelProfile.model_validate(_aggregated(members=["only-one"]))
        assert row.members == ["only-one"]
