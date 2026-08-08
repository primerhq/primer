"""Tests for primer.model.agent."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from primer.model.agent import Agent, AgentModel


# ---- AgentModel ---------------------------------------------------------


class TestAgentModel:
    def test_construction(self) -> None:
        """An agent names a ModelProfile; the provider and the wire model
        name live on that profile, not here."""
        m = AgentModel(profile_id="openai-1--gpt-4o-mini")
        assert m.profile_id == "openai-1--gpt-4o-mini"

    def test_profile_id_is_required(self) -> None:
        with pytest.raises(ValidationError):
            AgentModel()  # type: ignore[call-arg]

    def test_empty_profile_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentModel(profile_id="")

    def test_profile_id_is_opaque(self) -> None:
        """The '<provider>--<model>' shape is only what migration m002
        synthesises; an operator-created profile may use any id, so the
        model must not parse structure out of it."""
        m = AgentModel(profile_id="the-fast-one")
        assert m.profile_id == "the-fast-one"

    def test_round_trip_through_json(self) -> None:
        m = AgentModel(profile_id="anthropic-1--claude-sonnet-4-6")
        parsed = AgentModel.model_validate_json(m.model_dump_json())
        assert parsed == m


# ---- Agent --------------------------------------------------------------


class TestAgent:
    def test_minimal_construction(self) -> None:
        a = Agent(
            id="researcher",
            description="Finds slow tests in a repo.",
            model=AgentModel(profile_id="openai-1--gpt-4o-mini"),
        )
        assert a.id == "researcher"
        assert a.description == "Finds slow tests in a repo."
        assert a.model.profile_id == "openai-1--gpt-4o-mini"

    def test_defaults(self) -> None:
        a = Agent(
            id="r",
            description="x",
            model=AgentModel(profile_id="p--m"),
        )
        assert a.temperature is None
        assert a.tools == []
        assert a.system_prompt == []
        assert a.compaction_prompt == []

    def test_full_construction(self) -> None:
        a = Agent(
            id="researcher",
            description="A researcher agent",
            model=AgentModel(profile_id="openai-1--gpt-4o-mini"),
            temperature=0.7,
            tools=["web__search", "misc__calculate"],
            system_prompt=[
                "You are a thorough researcher.",
                "Always cite your sources.",
            ],
            compaction_prompt=[
                "Summarise the conversation while preserving cited sources.",
                "Drop intermediate tool results unless they were quoted.",
            ],
        )
        assert a.temperature == 0.7
        assert a.tools == ["web__search", "misc__calculate"]
        assert len(a.system_prompt) == 2
        assert len(a.compaction_prompt) == 2

    def test_temperature_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            Agent(
                id="r",
                description="x",
                model=AgentModel(profile_id="p--m"),
                temperature=-0.1,
            )

    def test_temperature_zero_allowed(self) -> None:
        a = Agent(
            id="r",
            description="x",
            model=AgentModel(profile_id="p--m"),
            temperature=0.0,
        )
        assert a.temperature == 0.0

    def test_temperature_above_one_allowed_for_openai_range(self) -> None:
        # No upper bound is enforced at the model level; clamping is the
        # adapter's job because providers disagree on the upper bound.
        a = Agent(
            id="r",
            description="x",
            model=AgentModel(profile_id="p--m"),
            temperature=1.8,
        )
        assert a.temperature == 1.8

    def test_empty_id_autogenerates_with_prefix(self) -> None:
        a = Agent(
            id="",
            description="x",
            model=AgentModel(profile_id="p--m"),
        )
        assert re.fullmatch(r"agent-[0-9a-f]{12}", a.id), a.id

    def test_description_inherited_from_describeable_allows_empty(self) -> None:
        # Describeable.description has no min_length constraint; the
        # field is free-form prose and may be empty in v1.
        a = Agent(
            id="r",
            description="",
            model=AgentModel(profile_id="p--m"),
        )
        assert a.description == ""

    def test_model_field_is_required(self) -> None:
        with pytest.raises(ValidationError):
            Agent(id="r", description="x")  # type: ignore[call-arg]

    def test_round_trip_through_json(self) -> None:
        a = Agent(
            id="researcher",
            description="A researcher agent",
            model=AgentModel(profile_id="openai-1--gpt-4o-mini"),
            temperature=0.5,
            tools=["system__search"],
            system_prompt=["Be terse."],
            compaction_prompt=["Keep the user's stated goal verbatim."],
        )
        parsed = Agent.model_validate_json(a.model_dump_json())
        assert parsed == a
        assert parsed.compaction_prompt == ["Keep the user's stated goal verbatim."]

    def test_tools_list_accepts_arbitrary_string_ids(self) -> None:
        # The model layer doesn't validate that tools resolve against
        # configured toolsets -- that's the runtime's job.
        a = Agent(
            id="r",
            description="x",
            model=AgentModel(profile_id="p--m"),
            tools=["never_registered_tool"],
        )
        assert a.tools == ["never_registered_tool"]
