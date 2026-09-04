"""ModelProfile aggregation: CRUD validation + reference integrity.

Supersedes ``tests/api/test_aggregated_llm_provider.py`` (deleted): the
aggregated concept moved off LLMProvider entirely, so its REST contract
now lives on ``/v1/model_profiles`` (POST/PUT with ``kind="aggregated"``)
rather than ``/v1/llm_providers`` (``provider="aggregated"``). One
behavior INVERTED in the move, not just relocated: a member's existence
used to be a deep check done lazily at resolve time ("Member existence
is a deep check done at resolve, not at write" -- the old file's own
comment); it is now validated EAGERLY at write time (create/update), so
what used to be accepted (``test_create_with_nonexistent_member_is_
accepted``) is now rejected (``test_nonexistent_member_is_422`` below).

Coverage: model_profiles.py's on_pre_create/on_pre_update aggregation
hooks (the CRUD-time checks a bare ModelProfile validator cannot do --
see test_model_profile_aggregated.py for the bare-model shape checks),
the ReferenceCheck extension blocking deletion of an in-use member, and
an explicit ModelProfile UPDATE-path test (there was none before this
suite -- the exact gap that let a latent on_pre_update signature
mismatch in _assert_provider_exists ship undetected).
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from primer.model.provider import AnthropicConfig, Limits, LLMProvider, LLMProviderType
from tests._support.model_profiles import profile_body, seed_profile


async def _seed_provider(client, provider_id: str) -> None:
    body = LLMProvider(
        id=provider_id,
        provider=LLMProviderType.ANTHROPIC,
        config=AnthropicConfig(api_key=SecretStr("sk-test")),
        limits=Limits(max_concurrency=4),
    ).model_dump(mode="json")
    r = await client.post("/v1/llm_providers", json=body)
    assert r.status_code in (200, 201), r.text


def _aggregated_body(profile_id: str, members: list[str], **overrides) -> dict:
    body = {
        "id": profile_id,
        "description": "an aggregated profile",
        "kind": "aggregated",
        "members": members,
    }
    body.update(overrides)
    return body


class TestCreateValidation:
    @pytest.mark.asyncio
    async def test_valid_aggregated_profile_creates(self, client) -> None:
        await _seed_provider(client, "magg-prov-1")
        m1 = await seed_profile(client, "magg-prov-1", "model-1")
        m2 = await seed_profile(client, "magg-prov-1", "model-2")

        r = await client.post(
            "/v1/model_profiles", json=_aggregated_body("magg-valid", [m1, m2]),
        )
        assert r.status_code in (200, 201), r.text
        assert r.json()["kind"] == "aggregated"
        assert r.json()["members"] == [m1, m2]
        assert r.json()["provider_id"] is None

    @pytest.mark.asyncio
    async def test_zero_members_is_422(self, client) -> None:
        r = await client.post(
            "/v1/model_profiles", json=_aggregated_body("magg-empty", []),
        )
        assert r.status_code == 422, r.text
        assert r.json()["extensions"]["error"] == "aggregation_too_small"

    @pytest.mark.asyncio
    async def test_one_member_is_422(self, client) -> None:
        """The min-members tightening: the old AggregatedLLMConfig allowed
        a single-member pool; the new shape requires >= 2 (the user's
        literal directive)."""
        await _seed_provider(client, "magg-prov-2")
        m1 = await seed_profile(client, "magg-prov-2", "model-1")

        r = await client.post(
            "/v1/model_profiles", json=_aggregated_body("magg-one", [m1]),
        )
        assert r.status_code == 422, r.text
        assert r.json()["extensions"]["error"] == "aggregation_too_small"

    @pytest.mark.asyncio
    async def test_nonexistent_member_is_422(self, client) -> None:
        """INVERTED from the old (provider-shaped) behavior: existence is
        now an eager, write-time check, not a lazy resolve-time one."""
        await _seed_provider(client, "magg-prov-3")
        m1 = await seed_profile(client, "magg-prov-3", "model-1")

        r = await client.post(
            "/v1/model_profiles",
            json=_aggregated_body("magg-ghost", [m1, "does-not-exist"]),
        )
        assert r.status_code == 422, r.text
        assert r.json()["extensions"]["error"] == "member_not_found"

    @pytest.mark.asyncio
    async def test_member_that_is_itself_aggregated_is_422(self, client) -> None:
        """Nested aggregation REJECTED eagerly (v1) -- closes the gap
        where the old AggregatedLLM only discovered this lazily, at
        resolve/stream time."""
        await _seed_provider(client, "magg-prov-4")
        m1 = await seed_profile(client, "magg-prov-4", "model-1")
        m2 = await seed_profile(client, "magg-prov-4", "model-2")
        r = await client.post(
            "/v1/model_profiles", json=_aggregated_body("magg-inner", [m1, m2]),
        )
        assert r.status_code in (200, 201), r.text

        m3 = await seed_profile(client, "magg-prov-4", "model-3")
        r = await client.post(
            "/v1/model_profiles",
            json=_aggregated_body("magg-outer", ["magg-inner", m3]),
        )
        assert r.status_code == 422, r.text
        assert r.json()["extensions"]["error"] == "nested_aggregation"

    @pytest.mark.asyncio
    async def test_self_reference_is_422(self, client) -> None:
        await _seed_provider(client, "magg-prov-5")
        m1 = await seed_profile(client, "magg-prov-5", "model-1")

        r = await client.post(
            "/v1/model_profiles",
            json=_aggregated_body("magg-self", [m1, "magg-self"]),
        )
        assert r.status_code == 422, r.text
        assert r.json()["extensions"]["error"] == "self_reference"

    @pytest.mark.asyncio
    async def test_duplicate_members_is_422(self, client) -> None:
        """Order is the routing/failover chain, so a duplicate is
        rejected outright rather than silently deduped -- deduping would
        silently change behaviour."""
        await _seed_provider(client, "magg-prov-6")
        m1 = await seed_profile(client, "magg-prov-6", "model-1")
        m2 = await seed_profile(client, "magg-prov-6", "model-2")

        r = await client.post(
            "/v1/model_profiles",
            json=_aggregated_body("magg-dup", [m1, m2, m1]),
        )
        assert r.status_code == 422, r.text
        assert r.json()["extensions"]["error"] == "duplicate_member"


class TestUpdateValidation:
    @pytest.mark.asyncio
    async def test_update_a_single_profiles_provider_id_succeeds(
        self, client,
    ) -> None:
        """Explicit ModelProfile UPDATE-path coverage -- there was NONE
        before this suite. Exercises on_pre_update end to end: before the
        fix in this same change, _assert_provider_exists was wired as
        on_pre_update with a 2-arg signature while the managed_by_field
        chain calls on_pre_update with 3 (entity, existing, request),
        which would raise TypeError on every PUT to this router. This
        pins that path stays working, not just that the new aggregation
        checks are correct.
        """
        await _seed_provider(client, "magg-prov-upd-a")
        await _seed_provider(client, "magg-prov-upd-b")
        pid = await seed_profile(client, "magg-prov-upd-a", "model-1")

        body = profile_body("magg-prov-upd-b", "model-1")
        body["id"] = pid
        r = await client.put(f"/v1/model_profiles/{pid}", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["provider_id"] == "magg-prov-upd-b"

    @pytest.mark.asyncio
    async def test_update_with_nonexistent_provider_is_422(self, client) -> None:
        await _seed_provider(client, "magg-prov-upd-c")
        pid = await seed_profile(client, "magg-prov-upd-c", "model-1")

        body = profile_body("ghost-provider", "model-1")
        body["id"] = pid
        r = await client.put(f"/v1/model_profiles/{pid}", json=body)
        assert r.status_code == 422, r.text
        assert r.json()["extensions"]["error"] == "provider_not_found"

    @pytest.mark.asyncio
    async def test_update_to_aggregated_validates_members(self, client) -> None:
        """The aggregation checks apply on update, not just create."""
        await _seed_provider(client, "magg-prov-upd-d")
        pid = await seed_profile(client, "magg-prov-upd-d", "model-1")

        r = await client.put(
            f"/v1/model_profiles/{pid}",
            json=_aggregated_body(pid, ["does-not-exist", "also-missing"]),
        )
        assert r.status_code == 422, r.text
        assert r.json()["extensions"]["error"] == "member_not_found"

    @pytest.mark.asyncio
    async def test_update_to_valid_aggregation_succeeds(self, client) -> None:
        await _seed_provider(client, "magg-prov-upd-e")
        pid = await seed_profile(client, "magg-prov-upd-e", "model-to-convert")
        m1 = await seed_profile(client, "magg-prov-upd-e", "model-1")
        m2 = await seed_profile(client, "magg-prov-upd-e", "model-2")

        r = await client.put(
            f"/v1/model_profiles/{pid}", json=_aggregated_body(pid, [m1, m2]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "aggregated"
        assert r.json()["provider_id"] is None

    @pytest.mark.asyncio
    async def test_update_a_member_to_aggregated_is_blocked(self, client) -> None:
        """01a067c4 gate finding #5: PUT'ing a profile that is currently a
        MEMBER of some other aggregate to kind="aggregated" itself must
        be rejected -- it would leave the containing aggregate silently
        violating "every member must be kind=single" (nested
        aggregation), discovered only later at resolve time with an
        error that misattributes the problem to the containing aggregate
        instead of this update."""
        await _seed_provider(client, "magg-prov-upd-f")
        m1 = await seed_profile(client, "magg-prov-upd-f", "model-1")
        m2 = await seed_profile(client, "magg-prov-upd-f", "model-2")
        r = await client.post(
            "/v1/model_profiles", json=_aggregated_body("magg-containing", [m1, m2]),
        )
        assert r.status_code in (200, 201), r.text

        # m1 is a member of magg-containing; try to convert it in place.
        other1 = await seed_profile(client, "magg-prov-upd-f", "other-1")
        other2 = await seed_profile(client, "magg-prov-upd-f", "other-2")
        r = await client.put(
            f"/v1/model_profiles/{m1}",
            json=_aggregated_body(m1, [other1, other2]),
        )
        assert r.status_code == 422, r.text
        assert r.json()["extensions"]["error"] == "member_of_another_aggregate"

        # m1 itself is unchanged -- still a single profile.
        got = await client.get(f"/v1/model_profiles/{m1}")
        assert got.json()["kind"] == "single"


class TestReferenceIntegrity:
    @pytest.mark.asyncio
    async def test_deleting_a_member_profile_is_blocked(self, client) -> None:
        await _seed_provider(client, "magg-prov-ref-1")
        m1 = await seed_profile(client, "magg-prov-ref-1", "model-1")
        m2 = await seed_profile(client, "magg-prov-ref-1", "model-2")
        r = await client.post(
            "/v1/model_profiles", json=_aggregated_body("magg-ref-agg", [m1, m2]),
        )
        assert r.status_code in (200, 201), r.text

        r = await client.delete(f"/v1/model_profiles/{m1}")
        assert r.status_code == 409, r.text
        assert "in_use_by" in r.text

        # Cleanup: delete the aggregate first so the member can go too.
        await client.delete("/v1/model_profiles/magg-ref-agg")
        await client.delete(f"/v1/model_profiles/{m1}")
        await client.delete(f"/v1/model_profiles/{m2}")

    @pytest.mark.asyncio
    async def test_deleting_a_non_member_profile_succeeds(self, client) -> None:
        await _seed_provider(client, "magg-prov-ref-2")
        m1 = await seed_profile(client, "magg-prov-ref-2", "model-1")
        m2 = await seed_profile(client, "magg-prov-ref-2", "model-2")
        unrelated = await seed_profile(client, "magg-prov-ref-2", "model-unrelated")
        r = await client.post(
            "/v1/model_profiles",
            json=_aggregated_body("magg-ref-agg-2", [m1, m2]),
        )
        assert r.status_code in (200, 201), r.text

        r = await client.delete(f"/v1/model_profiles/{unrelated}")
        assert r.status_code == 204, r.text

        await client.delete("/v1/model_profiles/magg-ref-agg-2")
        await client.delete(f"/v1/model_profiles/{m1}")
        await client.delete(f"/v1/model_profiles/{m2}")


class TestAggregatedCacheInvalidation:
    """01a067c4 gate finding #2's router-side half: model_profiles.py's
    on_update/on_delete hooks must drop ProviderRegistry's cached
    AggregatedLLM for a profile id, or an edit to the routing policy /
    member list (or the row's outright deletion) would keep being served
    by a stale cached instance until the process restarts. The registry
    cache's own lock/generation-guard/invalidation-bus machinery is
    covered directly in test_provider_registry.py /
    test_provider_registry_invalidation.py -- this only pins that the
    CRUD router actually calls it, end to end through the REST layer.
    """

    @pytest.mark.asyncio
    async def test_update_invalidates_the_cached_aggregate(
        self, client, app,
    ) -> None:
        await _seed_provider(client, "magg-prov-inv-1")
        m1 = await seed_profile(client, "magg-prov-inv-1", "model-1")
        m2 = await seed_profile(client, "magg-prov-inv-1", "model-2")
        m3 = await seed_profile(client, "magg-prov-inv-1", "model-3")
        r = await client.post(
            "/v1/model_profiles", json=_aggregated_body("magg-inv-1", [m1, m2]),
        )
        assert r.status_code in (200, 201), r.text

        registry = app.state.provider_registry
        # Populate the cache directly (a real resolve_llm() call would do
        # this too, but this keeps the test focused on the invalidation
        # wiring rather than also exercising AggregatedLLM construction).
        sentinel = object()
        registry._aggregated_llm_cache["magg-inv-1"] = sentinel
        assert "magg-inv-1" in registry._aggregated_llm_cache

        r = await client.put(
            "/v1/model_profiles/magg-inv-1",
            json=_aggregated_body("magg-inv-1", [m1, m2, m3]),
        )
        assert r.status_code == 200, r.text
        assert "magg-inv-1" not in registry._aggregated_llm_cache

    @pytest.mark.asyncio
    async def test_delete_invalidates_the_cached_aggregate(
        self, client, app,
    ) -> None:
        await _seed_provider(client, "magg-prov-inv-2")
        m1 = await seed_profile(client, "magg-prov-inv-2", "model-1")
        m2 = await seed_profile(client, "magg-prov-inv-2", "model-2")
        r = await client.post(
            "/v1/model_profiles", json=_aggregated_body("magg-inv-2", [m1, m2]),
        )
        assert r.status_code in (200, 201), r.text

        registry = app.state.provider_registry
        sentinel = object()
        registry._aggregated_llm_cache["magg-inv-2"] = sentinel

        r = await client.delete("/v1/model_profiles/magg-inv-2")
        assert r.status_code == 204, r.text
        assert "magg-inv-2" not in registry._aggregated_llm_cache

    @pytest.mark.asyncio
    async def test_invalidation_hook_is_a_noop_for_an_uncached_single_profile(
        self, client, app,
    ) -> None:
        """Wired unconditionally on every update/delete, single or
        aggregated, per Dev-Backend's reasoning: popping a key that was
        never cached is a no-op, not an error."""
        await _seed_provider(client, "magg-prov-inv-3")
        pid = await seed_profile(client, "magg-prov-inv-3", "model-1")

        r = await client.put(
            f"/v1/model_profiles/{pid}",
            json=profile_body("magg-prov-inv-3", "model-1", context_length=4096),
        )
        assert r.status_code == 200, r.text
        r = await client.delete(f"/v1/model_profiles/{pid}")
        assert r.status_code == 204, r.text
