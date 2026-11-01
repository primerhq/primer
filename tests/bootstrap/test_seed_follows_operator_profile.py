"""GI-2: a re-created seeded agent binds to the profile the install's operator agent uses, and only an install that was never seeded publishes a profile."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from primer.bootstrap.defaults import RESERVED_OPERATOR_AGENT
from primer.bootstrap.operator_defaults import SEEDED_AGENT_FACTORIES
from primer.bootstrap.seed import ensure_seeded_agents
from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.model_profile import ModelProfile
from primer.model.provider import AnthropicConfig, Limits, LLMProvider, LLMProviderType
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _profile(sp, profile_id: str) -> None:
    await sp.get_storage(LLMProvider).create(
        LLMProvider(id=f"prov-{profile_id}", provider=LLMProviderType.ANTHROPIC, config=AnthropicConfig(api_key=SecretStr("k")),
                    limits=Limits(max_concurrency=1))
    )
    await sp.get_storage(ModelProfile).create(
        ModelProfile(id=profile_id, description="d", provider_id=f"prov-{profile_id}", model_name="m", context_length=1000)
    )


async def _bound(sp, agent_id: str) -> str:
    return (await sp.get_storage(Agent).get(agent_id)).model.profile_id


async def test_a_recreated_agent_takes_the_operators_profile(sp):
    await _profile(sp, "seeded")
    await ensure_seeded_agents(sp)
    await _profile(sp, "aaa-private")                      # an admin's later profile, which lists first
    await sp.get_storage(Agent).delete("explorer")

    created = await ensure_seeded_agents(sp)

    assert created == ["explorer"] and await _bound(sp, "explorer") == "seeded"


async def test_with_no_operator_row_another_seeded_agents_profile_is_used_then_the_first(sp):
    await _profile(sp, "seeded")
    await ensure_seeded_agents(sp)
    await _profile(sp, "aaa-private")
    agents = sp.get_storage(Agent)
    await agents.delete(RESERVED_OPERATOR_AGENT)
    await ensure_seeded_agents(sp)
    assert await _bound(sp, RESERVED_OPERATOR_AGENT) == "seeded"          # the builder (or any survivor) still names it

    for agent_id in SEEDED_AGENT_FACTORIES:
        await agents.delete(agent_id)
    await ensure_seeded_agents(sp)
    assert await _bound(sp, RESERVED_OPERATOR_AGENT) == "aaa-private"     # no seeded agent exists: the first profile, as on a fresh install


async def test_deleting_the_whole_roster_publishes_nothing(sp):
    await _profile(sp, "seeded")
    await ensure_seeded_agents(sp)
    profiles = sp.get_storage(ModelProfile)
    published = await profiles.get("seeded")
    assert published.access.public
    await profiles.update(published.model_copy(update={"access": Access(public=False, rev=5)}))   # the admin made it private again
    await _profile(sp, "aaa-private")
    for agent_id in SEEDED_AGENT_FACTORIES:
        await sp.get_storage(Agent).delete(agent_id)

    await ensure_seeded_agents(sp)

    assert not (await profiles.get("aaa-private")).access.public and not (await profiles.get("seeded")).access.public
    assert not (await sp.get_storage(LLMProvider).get("prov-aaa-private")).access.public


async def test_an_install_seeded_before_the_marker_publishes_nothing_either(sp):
    await _profile(sp, "seeded")
    await ensure_seeded_agents(sp)
    from primer.bootstrap import seed as seed_mod

    await sp.get_storage(seed_mod.SeedMarker).delete(seed_mod.ROSTER_MARKER_ID)   # what an upgraded install looks like: agents, no marker
    await _profile(sp, "aaa-private")
    await ensure_seeded_agents(sp)
    assert not (await sp.get_storage(ModelProfile).get("aaa-private")).access.public
    assert await sp.get_storage(seed_mod.SeedMarker).get(seed_mod.ROSTER_MARKER_ID) is not None   # and the marker is written now


async def test_a_fresh_install_still_publishes_its_profile(sp):
    await _profile(sp, "seeded")
    await ensure_seeded_agents(sp)
    assert (await sp.get_storage(ModelProfile).get("seeded")).access.public
