"""``resolve_llm(person=)`` is a USE: the profile the run names and every LLM provider behind it (access-control P4 Task 4).

A grant on an aggregate covers its members, a grant on a member never covers the aggregate, the override (a session's or a graph node's) is
the profile that is checked, and a provider the person may not use refuses the run whichever profile reaches it.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from primer.access.person import Person
from primer.authz import PROFILE_UNUSABLE_CODE, ProfileUnusable
from primer.model.access import Access
from primer.model.model_profile import ModelProfile
from primer.model.provider import AnthropicConfig, Limits, LLMProvider, LLMProviderType
from primer.model_profile.resolver import resolve_llm

ADA, ROOT = Person("ada", "user"), Person("root", "admin")
GRANT_ADA = Access(grants={"ada": "read"}, rev=1)
PUBLIC = Access(public=True)
PRIVATE = Access()


class _Registry:
    async def get_llm(self, provider_id):
        return f"llm:{provider_id}"

    async def get_aggregated_llm(self, profile_id, *, resolve_member):
        return f"agg:{profile_id}"


async def _provider(sp, pid: str, access: Access = PUBLIC) -> None:
    await sp.get_storage(LLMProvider).create(
        LLMProvider(
            id=pid, provider=LLMProviderType.ANTHROPIC, config=AnthropicConfig(api_key=SecretStr("k")),
            limits=Limits(max_concurrency=1), access=access,
        )
    )


async def _profile(sp, pid: str, provider_id: str = "prov", access: Access = PUBLIC) -> None:
    await sp.get_storage(ModelProfile).create(
        ModelProfile(id=pid, description=pid, provider_id=provider_id, model_name="m", context_length=1000, access=access)
    )


async def _aggregate(sp, pid: str, members: list[str], access: Access = PUBLIC) -> None:
    await sp.get_storage(ModelProfile).create(ModelProfile(id=pid, description=pid, kind="aggregated", members=members, access=access))


async def _resolve(sp, person, default: str, override: str | None = None):
    return await resolve_llm(sp, _Registry(), default_profile_id=default, override_profile_id=override, person=person)


async def test_a_usable_profile_on_a_usable_provider_runs(fake_storage_provider):
    sp = fake_storage_provider
    await _provider(sp, "prov")
    await _profile(sp, "pub", access=PUBLIC)
    await _profile(sp, "granted", access=GRANT_ADA)

    assert (await _resolve(sp, ADA, "pub"))[0] == "llm:prov"
    assert (await _resolve(sp, ADA, "granted"))[0] == "llm:prov"


async def test_a_private_profile_is_refused_for_a_person_but_not_an_admin_or_an_unchecked_caller(fake_storage_provider):
    sp = fake_storage_provider
    await _provider(sp, "prov")
    await _profile(sp, "private", access=PRIVATE)

    with pytest.raises(ProfileUnusable) as caught:
        await _resolve(sp, ADA, "private")
    assert caught.value.code == PROFILE_UNUSABLE_CODE and "'private'" in caught.value.message
    assert (await _resolve(sp, ROOT, "private"))[0] == "llm:prov"
    assert (await _resolve(sp, None, "private"))[0] == "llm:prov"  # no person: nothing is checked


async def test_a_provider_the_person_may_not_use_refuses_the_run(fake_storage_provider):
    sp = fake_storage_provider
    await _provider(sp, "prov-private", PRIVATE)
    await _provider(sp, "prov-granted", GRANT_ADA)
    await _profile(sp, "on-private", provider_id="prov-private", access=PUBLIC)
    await _profile(sp, "on-granted", provider_id="prov-granted", access=PUBLIC)

    with pytest.raises(ProfileUnusable):
        await _resolve(sp, ADA, "on-private")
    assert (await _resolve(sp, ADA, "on-granted"))[0] == "llm:prov-granted"


async def test_grant_on_aggregate_covers_members(fake_storage_provider):
    sp = fake_storage_provider
    await _provider(sp, "prov")
    await _profile(sp, "m1", access=PRIVATE)
    await _profile(sp, "m2", access=PRIVATE)
    await _aggregate(sp, "agg", ["m1", "m2"], access=GRANT_ADA)

    llm, resolved = await _resolve(sp, ADA, "agg")

    assert llm == "agg:agg" and resolved.profile_id == "agg"


async def test_member_grant_does_not_cover_the_aggregate(fake_storage_provider):
    sp = fake_storage_provider
    await _provider(sp, "prov")
    await _profile(sp, "m1", access=GRANT_ADA)
    await _profile(sp, "m2", access=GRANT_ADA)
    await _aggregate(sp, "agg", ["m1", "m2"], access=PRIVATE)

    with pytest.raises(ProfileUnusable):
        await _resolve(sp, ADA, "agg")
    assert (await _resolve(sp, ADA, "m1"))[0] == "llm:prov"  # the member itself is hers


async def test_an_aggregate_is_refused_when_a_member_sits_on_a_provider_the_person_may_not_use(fake_storage_provider):
    sp = fake_storage_provider
    await _provider(sp, "prov-ok")
    await _provider(sp, "prov-private", PRIVATE)
    await _profile(sp, "m1", provider_id="prov-ok")
    await _profile(sp, "m2", provider_id="prov-private")
    await _aggregate(sp, "agg", ["m1", "m2"], access=PUBLIC)

    with pytest.raises(ProfileUnusable):
        await _resolve(sp, ADA, "agg")
    assert (await _resolve(sp, ROOT, "agg"))[0] == "agg:agg"


async def test_override_is_the_checked_profile(fake_storage_provider):
    sp = fake_storage_provider
    await _provider(sp, "prov")
    await _profile(sp, "default", access=PUBLIC)
    await _profile(sp, "override", access=PRIVATE)

    with pytest.raises(ProfileUnusable) as caught:
        await _resolve(sp, ADA, "default", override="override")
    assert "'override'" in caught.value.message
    assert (await _resolve(sp, ADA, "override", override="default"))[0] == "llm:prov"  # the winner is the override
