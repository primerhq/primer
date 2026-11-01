"""GX-16 (user ruling: profile types are not interconvertible): a model profile's kind is fixed at creation.

An update that changes ``kind`` either way answers 422 ``profile_kind_immutable`` (field ``kind``) from every write door that shares
``check_profile_on_update``; the row is unchanged. Editing any other field of either kind works as before.
"""

from __future__ import annotations

import pytest

from tests._support.model_profiles import seed_profile
from tests.api.test_model_profiles_aggregation import _aggregated_body, _seed_provider


async def _world(client, prefix: str):
    await _seed_provider(client, f"{prefix}-prov")
    m1 = await seed_profile(client, f"{prefix}-prov", "model-1")
    m2 = await seed_profile(client, f"{prefix}-prov", "model-2")
    agg = await client.post("/v1/model_profiles", json=_aggregated_body(f"{prefix}-agg", [m1, m2]))
    assert agg.status_code in (200, 201), agg.text
    return m1, m2, f"{prefix}-agg"


@pytest.mark.asyncio
async def test_both_kinds_are_created_and_their_other_fields_are_edited(client) -> None:
    m1, m2, agg = await _world(client, "kind-ok")
    single = (await client.get(f"/v1/model_profiles/{m1}")).json()
    single["description"] = "edited single"
    assert (await client.put(f"/v1/model_profiles/{m1}", json=single)).status_code == 200
    aggregate = (await client.get(f"/v1/model_profiles/{agg}")).json()
    aggregate["description"] = "edited aggregate"
    aggregate["members"] = [m2, m1]
    r = await client.put(f"/v1/model_profiles/{agg}", json=aggregate)
    assert r.status_code == 200, r.text
    assert (await client.get(f"/v1/model_profiles/{m1}")).json()["description"] == "edited single"
    assert (await client.get(f"/v1/model_profiles/{agg}")).json()["members"] == [m2, m1]


@pytest.mark.asyncio
async def test_a_single_cannot_become_an_aggregate(client) -> None:
    m1, m2, _agg = await _world(client, "kind-s2a")
    m3 = await seed_profile(client, "kind-s2a-prov", "model-3")
    r = await client.put(f"/v1/model_profiles/{m3}", json=_aggregated_body(m3, [m1, m2]))
    assert r.status_code == 422, r.text
    ext = r.json()["extensions"]
    assert ext["error"] == "profile_kind_immutable" and ext["field"] == "kind"
    assert "create a new profile" in ext["message"]
    assert (await client.get(f"/v1/model_profiles/{m3}")).json()["kind"] == "single"


@pytest.mark.asyncio
async def test_an_aggregate_cannot_become_a_single(client) -> None:
    m1, _m2, agg = await _world(client, "kind-a2s")
    body = (await client.get(f"/v1/model_profiles/{m1}")).json() | {"id": agg}
    r = await client.put(f"/v1/model_profiles/{agg}", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["extensions"]["error"] == "profile_kind_immutable"
    assert (await client.get(f"/v1/model_profiles/{agg}")).json()["kind"] == "aggregated"


@pytest.mark.asyncio
async def test_a_listed_member_still_cannot_become_an_aggregate_by_the_same_refusal(client) -> None:
    """Formerly 'member_of_another_aggregate' (nested aggregation); a member is single and its kind can no longer change, so the race is gone."""
    m1, m2, _agg = await _world(client, "kind-member")
    r = await client.put(f"/v1/model_profiles/{m1}", json=_aggregated_body(m1, [m2, m1 + "-x"]))
    assert r.status_code == 422 and r.json()["extensions"]["error"] == "profile_kind_immutable"
