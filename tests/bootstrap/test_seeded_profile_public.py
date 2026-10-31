"""The profile the public seeded agents run on is made public by the seed (access-control P4 Task 3, plan Decision 3).

A new profile is private; the seeded operator and builder are public agents bound to the default profile, so the ensure pass publishes that
profile and the LLM provider it resolves to. Only rows no admin ever shared (rev 0) are touched, so a re-run never undoes a choice.
"""

from __future__ import annotations

from pydantic import SecretStr

from primer.bootstrap.seed import default_profile_id, ensure_seeded_agents, publish_default_profile
from primer.model.access import Access
from primer.model.model_profile import ModelProfile
from primer.model.provider import AnthropicConfig, Limits, LLMProvider, LLMProviderType


async def _world(sp, profile_access: Access | None = None) -> None:
    await sp.get_storage(LLMProvider).create(
        LLMProvider(id="prov", provider=LLMProviderType.ANTHROPIC, config=AnthropicConfig(api_key=SecretStr("k")), limits=Limits(max_concurrency=1))
    )
    row = ModelProfile(id="prof", description="d", provider_id="prov", model_name="m", context_length=1000)
    await sp.get_storage(ModelProfile).create(row if profile_access is None else row.model_copy(update={"access": profile_access}))


async def test_the_default_profile_and_its_provider_become_public(fake_storage_provider):
    sp = fake_storage_provider
    sp.get_storage(ModelProfile)._data.clear()  # the fixture seeds profiles of its own; the seed binds agents to the first one it finds
    await _world(sp)
    default = await sp.get_storage(ModelProfile).get(await default_profile_id(sp))
    assert default.id == "prof" and not default.access.public

    await ensure_seeded_agents(sp)

    published = await sp.get_storage(ModelProfile).get(default.id)
    assert published.access.public
    assert (await sp.get_storage(LLMProvider).get(published.provider_id)).access.public


async def test_an_aggregate_publishes_its_members_and_their_providers(fake_storage_provider):
    sp = fake_storage_provider
    await _world(sp)
    await sp.get_storage(ModelProfile).create(ModelProfile(id="agg", description="a", kind="aggregated", members=["prof", "prof"]))

    await publish_default_profile(sp, "agg")

    for model, rid in ((ModelProfile, "agg"), (ModelProfile, "prof"), (LLMProvider, "prov")):
        assert (await sp.get_storage(model).get(rid)).access.public, rid


async def test_a_row_an_admin_already_shared_is_left_alone(fake_storage_provider):
    sp = fake_storage_provider
    await _world(sp, Access(grants={"u1": "read"}, rev=2))

    await publish_default_profile(sp, "prof")
    await publish_default_profile(sp, "prof")

    stored = (await sp.get_storage(ModelProfile).get("prof")).access
    assert not stored.public and stored.grants == {"u1": "read"} and stored.rev == 2


async def test_a_later_boot_publishes_no_profile(tmp_path):
    """Security review finding 1: only the install's setup publishes. Once the seeded agents exist, a boot or a ``/setup/seed`` re-run
    publishes nothing, not even a newer private profile that lists first (by id) and its private provider."""
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    try:
        await _world(sp)
        await ensure_seeded_agents(sp)  # the install's setup: the seeded agents are written, bound to "prof"
        assert (await sp.get_storage(ModelProfile).get("prof")).access.public

        await sp.get_storage(LLMProvider).create(
            LLMProvider(id="a-gpu", provider=LLMProviderType.ANTHROPIC, config=AnthropicConfig(api_key=SecretStr("k")), limits=Limits(max_concurrency=1))
        )
        await sp.get_storage(ModelProfile).create(
            ModelProfile(id="a-opus", description="d", provider_id="a-gpu", model_name="m", context_length=1000)
        )
        assert await default_profile_id(sp) == "a-opus"  # lists first

        await ensure_seeded_agents(sp)  # a later boot

        assert not (await sp.get_storage(ModelProfile).get("a-opus")).access.public
        assert not (await sp.get_storage(LLMProvider).get("a-gpu")).access.public
    finally:
        await sp.aclose()
