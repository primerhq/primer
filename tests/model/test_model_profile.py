"""Tests for the ModelProfile entity and its config block."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.model_profile import (
    ModelProfile,
    ModelProfileConfig,
    ReasoningLevel,
)


def _profile(**overrides: object) -> ModelProfile:
    payload: dict[str, object] = {
        "id": "gx10-qwen-fast",
        "description": "Qwen with reasoning suppressed for cheap turns.",
        "provider_id": "gx10",
        "model_name": "qwen",
        "context_length": 262144,
    }
    payload.update(overrides)
    return ModelProfile(**payload)  # type: ignore[arg-type]


class TestReasoningLevel:
    def test_values(self) -> None:
        assert [level.value for level in ReasoningLevel] == [
            "off", "minimal", "low", "medium", "high",
        ]

    def test_is_str_enum_so_it_serialises_as_its_value(self) -> None:
        assert ReasoningLevel.OFF == "off"


class TestModelProfileConfig:
    def test_defaults_are_inert(self) -> None:
        """A profile with no config must behave exactly like today."""
        cfg = ModelProfileConfig()
        assert cfg.reasoning is None
        assert cfg.extended == {}

    def test_extended_accepts_arbitrary_json(self) -> None:
        cfg = ModelProfileConfig(
            extended={"chat_template_kwargs": {"enable_thinking": False}}
        )
        assert cfg.extended["chat_template_kwargs"]["enable_thinking"] is False

    def test_reasoning_rejects_unknown_level(self) -> None:
        with pytest.raises(ValidationError):
            ModelProfileConfig(reasoning="ludicrous")  # type: ignore[arg-type]

    def test_extended_default_is_not_shared_between_instances(self) -> None:
        a = ModelProfileConfig()
        b = ModelProfileConfig()
        a.extended["k"] = "v"
        assert b.extended == {}


class TestModelProfile:
    def test_minimal_profile(self) -> None:
        p = _profile()
        assert p.config.reasoning is None
        assert p.config.extended == {}
        assert p.harness_id is None

    def test_id_prefix(self) -> None:
        assert ModelProfile._id_prefix == "model-profile"

    def test_context_length_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            _profile(context_length=0)

    def test_provider_id_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            _profile(provider_id="")

    def test_model_name_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            _profile(model_name="")

    def test_description_is_required(self) -> None:
        with pytest.raises(ValidationError):
            ModelProfile(  # type: ignore[call-arg]
                id="p", provider_id="g", model_name="m", context_length=1,
            )

    def test_two_profiles_may_share_a_model_on_one_provider(self) -> None:
        """The whole point: same (provider, model), different config."""
        fast = _profile(
            id="gx10-qwen-fast",
            config=ModelProfileConfig(reasoning=ReasoningLevel.OFF),
        )
        think = _profile(
            id="gx10-qwen-think",
            config=ModelProfileConfig(reasoning=ReasoningLevel.HIGH),
        )
        assert fast.provider_id == think.provider_id
        assert fast.model_name == think.model_name
        assert fast.id != think.id
        assert fast.config.reasoning is ReasoningLevel.OFF
        assert think.config.reasoning is ReasoningLevel.HIGH

    def test_round_trips_through_json(self) -> None:
        p = _profile(
            config=ModelProfileConfig(
                reasoning=ReasoningLevel.MEDIUM,
                extended={"seed": 7},
            )
        )
        restored = ModelProfile.model_validate(p.model_dump(mode="json"))
        assert restored == p
