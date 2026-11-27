"""``ModelProfileConfig.reasoning``: the presets and the hand-written custom value.

The presets (``off`` to ``max``) are mapped per wire and model (spec section 4);
the custom value (``{"custom": "..."}``) is a hand-written value sent verbatim
to the wire's custom reasoning target, with no mapping or clamping.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.model_profile import CustomReasoning, ModelProfileConfig, ReasoningLevel


class TestPresetLevels:
    def test_every_preset_value_parses_to_its_member(self) -> None:
        for level in ReasoningLevel:
            assert ModelProfileConfig(reasoning=level.value).reasoning is level

    def test_a_value_outside_the_presets_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ModelProfileConfig(reasoning="deeper")


class TestCustomValue:
    def test_a_custom_value_parses(self) -> None:
        cfg = ModelProfileConfig(reasoning={"custom": "ultra"})
        assert isinstance(cfg.reasoning, CustomReasoning)
        assert cfg.reasoning.custom == "ultra"

    @pytest.mark.parametrize("value", [
        "",        # empty
        "a" * 33,  # over 32 characters
        "x y",     # a space
        "café",    # outside the alphabet
    ])
    def test_a_value_outside_the_pattern_is_refused(self, value) -> None:
        with pytest.raises(ValidationError):
            ModelProfileConfig(reasoning={"custom": value})

    def test_an_extra_field_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ModelProfileConfig(reasoning={"custom": "high", "extra": 1})

    def test_the_json_round_trip_keeps_the_shape(self) -> None:
        cfg = ModelProfileConfig(reasoning={"custom": "ultra"})
        again = ModelProfileConfig.model_validate_json(cfg.model_dump_json())
        assert isinstance(again.reasoning, CustomReasoning)
        assert again.reasoning == cfg.reasoning
