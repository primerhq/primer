"""S5 P3: operator and builder are ensure-seeded once a profile exists."""
from __future__ import annotations

from primer.bootstrap.defaults import (
    RESERVED_EXPLORER_AGENT,
    RESERVED_PLANNER_AGENT,
    RESERVED_TOOL_RUNNER_AGENT,
    RESERVED_BUILDER_AGENT,
    RESERVED_OPERATOR_AGENT,
)
from primer.bootstrap.operator_defaults import BUILDER_TOOLS, OPERATOR_TOOLS
from primer.bootstrap.seed import ensure_seeded_agents
from primer.model.agent import Agent
from primer.model.model_profile import ModelProfile


async def _seed_profile(sp, profile_id="llm-1--qwen"):
    await sp.get_storage(ModelProfile).create(
        ModelProfile(
            id=profile_id,
            description="default",
            provider_id="llm-1",
            model_name="qwen",
            context_length=32000,
        )
    )
    return profile_id


async def test_no_profile_means_no_agents(fake_storage_provider):
    fake_storage_provider.get_storage(ModelProfile)._data.clear()
    assert await ensure_seeded_agents(fake_storage_provider) == []
    assert await fake_storage_provider.get_storage(Agent).get(
        RESERVED_OPERATOR_AGENT
    ) is None


async def test_the_next_pass_repairs_the_agents(fake_storage_provider):
    fake_storage_provider.get_storage(ModelProfile)._data.clear()
    await ensure_seeded_agents(fake_storage_provider)
    profile_id = await _seed_profile(fake_storage_provider)
    created = await ensure_seeded_agents(fake_storage_provider)
    assert set(created) == {
        RESERVED_OPERATOR_AGENT, RESERVED_BUILDER_AGENT,
        RESERVED_PLANNER_AGENT, RESERVED_EXPLORER_AGENT,
        RESERVED_TOOL_RUNNER_AGENT,
    }
    agents = fake_storage_provider.get_storage(Agent)
    operator = await agents.get(RESERVED_OPERATOR_AGENT)
    builder = await agents.get(RESERVED_BUILDER_AGENT)
    assert operator.model.profile_id == profile_id
    assert operator.tools == list(OPERATOR_TOOLS)
    assert builder.tools == list(BUILDER_TOOLS)
    assert "crud__create_agent" in builder.tools
    assert not any(t.startswith("crud__") for t in operator.tools)
    state = await fake_storage_provider.get_system_state()
    assert state.default_agent_id == RESERVED_OPERATOR_AGENT


async def test_user_edits_survive_the_next_pass(fake_storage_provider):
    fake_storage_provider.get_storage(ModelProfile)._data.clear()
    await _seed_profile(fake_storage_provider)
    await ensure_seeded_agents(fake_storage_provider)
    agents = fake_storage_provider.get_storage(Agent)
    operator = await agents.get(RESERVED_OPERATOR_AGENT)
    operator.system_prompt = ["my own prompt"]
    operator.tools = ["system__ask_user"]
    await agents.update(operator)
    assert await ensure_seeded_agents(fake_storage_provider) == []
    again = await agents.get(RESERVED_OPERATOR_AGENT)
    assert again.system_prompt == ["my own prompt"]
    assert again.tools == ["system__ask_user"]


async def test_a_deleted_builder_is_repaired(fake_storage_provider):
    fake_storage_provider.get_storage(ModelProfile)._data.clear()
    await _seed_profile(fake_storage_provider)
    await ensure_seeded_agents(fake_storage_provider)
    await fake_storage_provider.get_storage(Agent).delete(RESERVED_BUILDER_AGENT)
    assert await ensure_seeded_agents(fake_storage_provider) == [
        RESERVED_BUILDER_AGENT
    ]


# ---- the phase-1 roster ---------------------------------------------------


async def test_roster_grants_match_the_design(fake_storage_provider):
    """Each specialist gets exactly the spec table's tools, nothing more."""
    fake_storage_provider.get_storage(ModelProfile)._data.clear()
    await _seed_profile(fake_storage_provider)
    await ensure_seeded_agents(fake_storage_provider)
    agents = fake_storage_provider.get_storage(Agent)

    planner = await agents.get(RESERVED_PLANNER_AGENT)
    assert planner.tools == [
        "collections__search", "collections__read_document",
    ], "the planner grounds but never mutates"

    explorer = await agents.get(RESERVED_EXPLORER_AGENT)
    assert explorer.tools == [
        "collections__search", "collections__read_document",
        "collections__collection_tree",
    ]

    runner = await agents.get(RESERVED_TOOL_RUNNER_AGENT)
    assert runner.tools == [
        "collections__search", "system__call_tool",
    ], "vision ch.3: two meta-tools and nothing else"


async def test_a_deleted_planner_is_repaired(fake_storage_provider):
    fake_storage_provider.get_storage(ModelProfile)._data.clear()
    await _seed_profile(fake_storage_provider)
    await ensure_seeded_agents(fake_storage_provider)
    agents = fake_storage_provider.get_storage(Agent)
    await agents.delete(RESERVED_PLANNER_AGENT)
    created = await ensure_seeded_agents(fake_storage_provider)
    assert created == [RESERVED_PLANNER_AGENT]
    assert await agents.get(RESERVED_PLANNER_AGENT) is not None


async def test_descriptions_carry_the_advertisement_convention(
    fake_storage_provider,
):
    """Does X. Use when Y. Returns Z. - the searchable surface."""
    fake_storage_provider.get_storage(ModelProfile)._data.clear()
    await _seed_profile(fake_storage_provider)
    await ensure_seeded_agents(fake_storage_provider)
    agents = fake_storage_provider.get_storage(Agent)
    for agent_id in (RESERVED_PLANNER_AGENT, RESERVED_EXPLORER_AGENT,
                     RESERVED_TOOL_RUNNER_AGENT):
        row = await agents.get(agent_id)
        assert "Use when" in row.description, agent_id
        assert "Returns" in row.description, agent_id
