"""A full grantee cannot set ``harness_id`` on a collection through REST (access-control P3 SEC, finding 8): the owner stays able to share and delete it."""

from __future__ import annotations

from primer.model.collection import Collection
from tests.api.test_collection_access import ALICE, CAROL, CID, ROOT, _as, env  # noqa: F401


async def test_full_grantee_cannot_freeze_the_owners_collection(env) -> None:
    body = (await env.client.get(f"/v1/collections/{CID}", headers=_as(CAROL))).json()

    r = await env.client.put(f"/v1/collections/{CID}", headers=_as(CAROL), json={**body, "harness_id": "hns_does_not_exist"})

    assert r.status_code == 422, r.text
    assert (await env.sp.get_storage(Collection).get(CID)).harness_id is None
    rev = (await env.sp.get_storage(Collection).get(CID)).access.rev
    for who in (ALICE, ROOT):
        r = await env.client.put(f"/v1/collections/{CID}/access", headers=_as(who), json={"owners": [ALICE], "grants": {}, "rev": rev})
        assert r.status_code == 200, (who, r.text)
        rev = (await env.sp.get_storage(Collection).get(CID)).access.rev
