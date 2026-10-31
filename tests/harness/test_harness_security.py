"""Phase 3 security review findings on harnesses (access-control P3 SEC): ports of the reviewer's probes, asserting the SAFE behaviour.

* ``harness_id`` is never writable through a REST body: a full grantee cannot point someone's row at a harness of their own (the fan-out would hand it
  to them) or at one that does not exist (nothing could share the row again); a create cannot set it either.
* A row whose harness no longer exists (``apply_uninstall(cascade=False)`` leaves it) is shared directly again, so its grants can be revoked.
"""

from __future__ import annotations

from primer.harness.access import apply_harness_access
from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.harness import Harness
from tests.harness.test_harness_access import ALICE, BOB, NOW, ROOT, _agent, _as, env  # noqa: F401


async def _bobs_full_agent(env) -> dict:
    await env.sp.get_storage(Agent).create(_agent("ag-alice", harness_id=None, access=Access(owners=[ALICE], grants={BOB: "full"})))
    return (await env.client.get("/v1/agents/ag-alice", headers=_as(BOB))).json()


async def test_a_full_grantee_cannot_point_an_agent_at_a_harness(env) -> None:
    await env.sp.get_storage(Harness).create(
        Harness(id="hns_b", slug="bobs", name="B", git_url="https://github.com/x/z", created_at=NOW, access=Access(owners=[BOB]))
    )
    body = await _bobs_full_agent(env)

    for target in ("hns_b", "hns_does_not_exist"):
        r = await env.client.put("/v1/agents/ag-alice", headers=_as(BOB), json={**body, "harness_id": target})
        assert r.status_code == 422, (target, r.text)
        assert "harness_id" in r.text and "dispatch" in r.text, r.text
        assert (await env.sp.get_storage(Agent).get("ag-alice")).harness_id is None

    # The owner can still share and delete it.
    stored = await env.sp.get_storage(Agent).get("ag-alice")
    r = await env.client.put("/v1/agents/ag-alice/access", headers=_as(ALICE), json={"owners": [ALICE], "grants": {}, "rev": stored.access.rev})
    assert r.status_code == 200, r.text


async def test_an_unchanged_harness_id_in_a_put_body_is_not_refused(env) -> None:
    body = await _bobs_full_agent(env)
    r = await env.client.put("/v1/agents/ag-alice", headers=_as(BOB), json={**body, "description": "edited"})
    assert r.status_code == 200, r.text


async def test_a_create_cannot_set_harness_id(env) -> None:
    agent = _agent("ag-new", harness_id="hns_1", access=Access()).model_dump(mode="json")
    r = await env.client.post("/v1/agents", headers=_as(ALICE), json=agent)
    assert r.status_code == 422, r.text
    assert await env.sp.get_storage(Agent).get("ag-new") is None


async def test_a_row_whose_harness_is_gone_can_be_shared_and_revoked_again(env) -> None:
    sp = env.sp
    h = await sp.get_storage(Harness).get("hns_1")
    h2 = h.model_copy(update={"access": Access(owners=[ALICE], grants={BOB: "read"}, rev=1)})
    await sp.get_storage(Harness).update(h2)
    await apply_harness_access(sp, h2)
    assert BOB in (await sp.get_storage(Agent).get("ag-1")).access.grants
    await sp.get_storage(Harness).delete("hns_1")  # what apply_uninstall(cascade=False) leaves

    rev = (await sp.get_storage(Agent).get("ag-1")).access.rev
    r = await env.client.put("/v1/agents/ag-1/access", headers=_as(ROOT), json={"owners": [ALICE], "grants": {}, "rev": rev})
    assert r.status_code == 200, r.text
    assert (await env.client.get("/v1/agents/ag-1", headers=_as(BOB))).status_code == 404


async def test_a_row_whose_harness_exists_is_still_managed(env) -> None:
    rev = (await env.sp.get_storage(Agent).get("ag-1")).access.rev
    r = await env.client.put("/v1/agents/ag-1/access", headers=_as(ALICE), json={"owners": [ALICE], "grants": {BOB: "read"}, "rev": rev})
    assert r.status_code == 409 and "harness_managed" in r.text
