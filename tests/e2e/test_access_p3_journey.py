"""Two users and their collections, triggers, channel triggers and harnesses over the live API (access control, Phase 3).

A private collection is hidden from every REST door (the row, the tree, grep, search, the flat ``/v1/documents``); a read
share allows reads and refuses writes; a full share allows document writes but not the search toggles or the delete. A
trigger subscription is refused without full on the trigger and allowed after it. A channel trigger on another user's
private channel is refused at create. A harness is hidden until shared, and what it installed follows it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from tests._support.harness_git import build_harness_repo
from tests._support.model_profiles import profile_id_for, seed_llm_provider

_PASSWORD = "access-journey-password-123"


@asynccontextmanager
async def _user(client: httpx.AsyncClient, base_url: str, username: str) -> AsyncIterator[tuple[str, httpx.AsyncClient]]:
    made = await client.post("/v1/admin/users", json={"username": username, "password": _PASSWORD, "role": "user"})
    assert made.status_code == 201, made.text
    user_id = made.json()["id"]
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0)) as c:
            login = await c.post("/v1/auth/login", json={"username": username, "password": _PASSWORD})
            assert login.status_code == 200, login.text
            yield user_id, c
    finally:
        await client.delete(f"/v1/admin/users/{user_id}")


async def _share(owner: httpx.AsyncClient, plural: str, entity_id: str, owner_id: str, grants: dict[str, str]) -> None:
    current = (await owner.get(f"/v1/{plural}/{entity_id}/access")).json()
    shared = await owner.put(
        f"/v1/{plural}/{entity_id}/access", json={"owners": [owner_id], "grants": grants, "rev": current["rev"]},
    )
    assert shared.status_code == 200, shared.text


def _denied(r: httpx.Response) -> bool:
    return r.status_code == 403 and r.json().get("extensions", {}).get("code") == "access_denied"


async def test_a_private_collection_is_hidden_from_every_door_and_its_shares_grant_their_levels(client, base_url, unique_suffix) -> None:
    cid = f"acl-kb-{unique_suffix}"
    async with (
        _user(client, base_url, f"acl3-b-{unique_suffix}") as (b_id, b),
        _user(client, base_url, f"acl3-c-{unique_suffix}") as (c_id, c),
    ):
        made = await b.post("/v1/collections", json={"id": cid, "description": "access journey wiki"})
        assert made.status_code == 201, made.text
        assert made.json()["access"]["owners"] == [b_id]
        try:
            doc = await b.post(f"/v1/collections/{cid}/docs", json={"parent": "", "slug": "intro", "body": "zebrafish runbook"})
            assert doc.status_code == 201, doc.text
            doc_id = doc.json()["document"]["id"]

            # Every door answers like a missing collection for C.
            for method, url, kw in (
                ("GET", f"/v1/collections/{cid}", {}),
                ("GET", f"/v1/collections/{cid}/docs", {"params": {"path": "intro"}}),
                ("GET", f"/v1/collections/{cid}/docs", {"params": {"parent": ""}}),
                ("GET", f"/v1/collections/{cid}/grep", {"params": {"q": "zebrafish"}}),
                ("GET", f"/v1/collections/{cid}/documents", {}),
                ("POST", f"/v1/collections/{cid}/search", {"json": {"query": "zebrafish"}}),
                ("GET", f"/v1/collections/{cid}/search", {}),
                ("GET", f"/v1/documents/{doc_id}", {}),
            ):
                r = await c.request(method, url, **kw)
                assert r.status_code == 404, (method, url, r.text)
            assert cid not in [x["id"] for x in (await c.get("/v1/collections", params={"limit": 200})).json()["items"]]
            assert doc_id not in [x["id"] for x in (await c.get("/v1/documents", params={"limit": 200})).json()["items"]]
            found = await c.post(
                "/v1/documents/find",
                json={"page": {"kind": "offset", "offset": 0, "length": 50},
                      "predicate": {"left": {"kind": "field", "name": "collection_id"}, "op": "=", "right": {"kind": "value", "value": cid}}},
            )
            assert found.status_code == 200 and found.json()["items"] == [], found.text

            # Read share: C reads and greps, every write is 403 access_denied.
            await _share(b, "collections", cid, b_id, {c_id: "read"})
            read = await c.get(f"/v1/collections/{cid}/docs", params={"path": "intro"})
            assert read.status_code == 200 and read.json()["body"] == "zebrafish runbook", read.text
            assert (await c.get(f"/v1/collections/{cid}/grep", params={"q": "zebrafish"})).status_code == 200
            assert _denied(await c.post(f"/v1/collections/{cid}/docs", json={"parent": "", "slug": "c-note", "body": "x"}))
            assert _denied(await c.put(f"/v1/collections/{cid}/documents", params={"path": "x"}, json={"content": "x"}))

            # Full share: C writes documents, but the search toggles and the delete stay the owner's.
            await _share(b, "collections", cid, b_id, {c_id: "full"})
            wrote = await c.post(f"/v1/collections/{cid}/docs", json={"parent": "", "slug": "c-note", "body": "by c"})
            assert wrote.status_code == 201, wrote.text
            search = {"embedder": {"provider_id": "emb", "model": "m"}, "vector_store_provider_id": "ssp"}
            assert _denied(await c.put(f"/v1/collections/{cid}/search", json=search))
            assert _denied(await c.delete(f"/v1/collections/{cid}/search"))
            assert _denied(await c.delete(f"/v1/collections/{cid}"))
            # A whole-row update cannot change the search block, whoever sends it.
            refused = await c.put(f"/v1/collections/{cid}", json={"id": cid, "description": "d", "search": search})
            assert refused.status_code == 403, refused.text
            assert (await c.put(f"/v1/collections/{cid}", json={"id": cid, "description": "renamed by c"})).status_code == 200
        finally:
            await b.delete(f"/v1/collections/{cid}")


async def _a_workspace_of(user: httpx.AsyncClient) -> str:
    """A workspace ``user`` creates from a template it may read (the subscription target it holds full on)."""
    templates = (await user.get("/v1/workspace_templates", params={"limit": 200})).json().get("items", [])
    if not templates:
        pytest.skip("no workspace template this user may read on this server")
    made = await user.post("/v1/workspaces", json={"template_id": templates[0]["id"]})
    assert made.status_code == 201, made.text
    return made.json()["id"]


async def test_a_subscription_needs_full_on_the_trigger(client, base_url, unique_suffix) -> None:
    async with (
        _user(client, base_url, f"acl3-tb-{unique_suffix}") as (b_id, b),
        _user(client, base_url, f"acl3-tc-{unique_suffix}") as (c_id, c),
    ):
        made = await b.post(
            "/v1/triggers",
            json={"slug": f"acl3-{unique_suffix}", "name": "journey", "config": {"kind": "scheduled", "cron": "0 3 * * *"}, "enabled": False},
        )
        assert made.status_code == 201, made.text
        tid = made.json()["id"]
        workspace_id = await _a_workspace_of(c)
        sub = {"config": {"kind": "agent_fresh_session", "workspace_id": workspace_id, "agent_id": "operator"}}
        try:
            # No access: the trigger answers like a missing one. Read: 403. Full: the subscription is saved.
            assert (await c.post(f"/v1/triggers/{tid}/subscriptions", json=sub)).status_code == 404
            await _share(b, "triggers", tid, b_id, {c_id: "read"})
            assert (await c.get(f"/v1/triggers/{tid}")).status_code == 200
            assert _denied(await c.post(f"/v1/triggers/{tid}/subscriptions", json=sub))
            assert _denied(await c.post(f"/v1/triggers/{tid}/fire_now"))
            await _share(b, "triggers", tid, b_id, {c_id: "full"})
            saved = await c.post(f"/v1/triggers/{tid}/subscriptions", json=sub)
            assert saved.status_code == 201, saved.text
            assert _denied(await c.delete(f"/v1/triggers/{tid}"))
        finally:
            await b.delete(f"/v1/triggers/{tid}")
            await c.delete(f"/v1/workspaces/{workspace_id}")


async def test_a_channel_trigger_on_another_users_private_channel_is_refused(client, base_url, unique_suffix) -> None:
    cp_id = f"cp-acl3-{unique_suffix}"
    ch_id = f"ch-acl3-{unique_suffix}"
    provider = await client.post(
        "/v1/channel_providers",
        json={"id": cp_id, "provider": "slack",
              "config": {"app_token": "xapp-1-acl3", "bot_token": "xoxb-1-acl3", "signing_secret": "acl3-secret"}},
    )
    assert provider.status_code == 201, provider.text
    # A new channel provider is private to admins (access-control P4): the admin publishes it so the users below may bind channels to it.
    view = (await client.get(f"/v1/channel_providers/{cp_id}/access")).json()
    published = await client.put(f"/v1/channel_providers/{cp_id}/access", json={"owners": [], "grants": {}, "public": True, "rev": view["rev"]})
    assert published.status_code == 200, published.text
    try:
        async with (
            _user(client, base_url, f"acl3-cb-{unique_suffix}") as (b_id, b),
            _user(client, base_url, f"acl3-cc-{unique_suffix}") as (c_id, c),
        ):
            channel = await b.post("/v1/channels", json={"id": ch_id, "provider_id": cp_id, "provider": "slack", "external_id": f"C{unique_suffix}"})
            assert channel.status_code == 201, channel.text
            try:
                config = {"kind": "channel", "provider_id": cp_id, "channel_id": ch_id}
                refused = await c.post("/v1/triggers", json={"slug": f"acl3-ch-{unique_suffix}", "name": "c", "config": config})
                assert refused.status_code == 404, refused.text
                # A provider-wide channel trigger (no channel) is admin-only.
                wide = await c.post(
                    "/v1/triggers", json={"slug": f"acl3-w-{unique_suffix}", "name": "w", "config": {"kind": "channel", "provider_id": cp_id}},
                )
                assert _denied(wide), wide.text
                # Once B shares the channel to read, C may save a trigger on it.
                await _share(b, "channels", ch_id, b_id, {c_id: "read"})
                allowed = await c.post("/v1/triggers", json={"slug": f"acl3-ch-{unique_suffix}", "name": "c", "config": config})
                assert allowed.status_code == 201, allowed.text
                await c.delete(f"/v1/triggers/{allowed.json()['id']}")
            finally:
                await b.delete(f"/v1/channels/{ch_id}")
    finally:
        await client.delete(f"/v1/channel_providers/{cp_id}")


async def _register_harness(client: httpx.AsyncClient, url: str, suffix: str) -> str:
    made = await client.post(
        "/v1/harnesses",
        json={"slug": f"acl3h-{suffix}"[:64], "name": "access journey harness", "git_url": url, "ref": "main", "direction": "inbound"},
    )
    assert made.status_code in (200, 201), made.text
    return made.json()["id"]


async def _wait_idle(client: httpx.AsyncClient, harness_id: str, *, timeout_s: float = 30.0) -> dict:
    for _ in range(int(timeout_s / 0.3)):
        r = await client.get(f"/v1/harnesses/{harness_id}")
        if r.status_code == 200 and not r.json().get("pending_operation"):
            return r.json()
        await asyncio.sleep(0.3)
    return (await client.get(f"/v1/harnesses/{harness_id}")).json()


async def test_a_harness_is_hidden_until_shared(client, base_url, unique_suffix, tmp_path) -> None:
    harness_id = await _register_harness(client, build_harness_repo(tmp_path / "repo", name=f"h-{unique_suffix}"), unique_suffix)
    try:
        async with _user(client, base_url, f"acl3-h-{unique_suffix}") as (u_id, u):
            assert (await u.get(f"/v1/harnesses/{harness_id}")).status_code == 404
            assert harness_id not in [h["id"] for h in (await u.get("/v1/harnesses", params={"limit": 200})).json()["items"]]
            owners = (await client.get(f"/v1/harnesses/{harness_id}/access")).json()["owners"]
            shared = await client.put(
                f"/v1/harnesses/{harness_id}/access", json={"owners": [o["id"] for o in owners], "grants": {u_id: "read"}},
            )
            assert shared.status_code == 200, shared.text
            assert (await u.get(f"/v1/harnesses/{harness_id}")).status_code == 200
            # Harness writes stay admin-only even with access.
            assert (await u.post(f"/v1/harnesses/{harness_id}/fetch")).status_code == 403
    finally:
        await client.delete(f"/v1/harnesses/{harness_id}")


async def test_what_a_harness_installed_follows_its_sharing(client, base_url, unique_suffix, tmp_path) -> None:
    pid = f"acl3hp-{unique_suffix}"
    await seed_llm_provider(client, {
        "id": pid, "provider": "openchat", "models": [{"name": "scripted:default", "context_length": 8192}],
        "config": {"url": "http://127.0.0.1:1/v1", "flavor": "lmstudio"}, "limits": {"max_concurrency": 1},
    })
    harness_id = await _register_harness(client, build_harness_repo(tmp_path / "repo", name=f"h-{unique_suffix}"), unique_suffix)
    try:
        await client.post(f"/v1/harnesses/{harness_id}/fetch")
        await _wait_idle(client, harness_id)
        await client.put(f"/v1/harnesses/{harness_id}/overrides", json={"profile_id": profile_id_for(pid, "scripted:default")})
        await client.post(f"/v1/harnesses/{harness_id}/install")
        await _wait_idle(client, harness_id)
        installed = await client.post(
            "/v1/agents/find",
            json={"page": {"kind": "offset", "offset": 0, "length": 50},
                  "predicate": {"left": {"kind": "field", "name": "harness_id"}, "op": "=", "right": {"kind": "value", "value": harness_id}}},
        )
        agent_ids = [a["id"] for a in installed.json().get("items", [])]
        if not agent_ids:
            pytest.skip("the install did not materialise entities on this server (see test_smk_harness.py, F3)")
        async with _user(client, base_url, f"acl3-hi-{unique_suffix}") as (u_id, u):
            for agent_id in agent_ids:
                assert (await u.get(f"/v1/agents/{agent_id}")).status_code == 404
            # Sharing an installed row directly is refused: it is shared through its harness.
            direct = await client.put(f"/v1/agents/{agent_ids[0]}/access", json={"owners": [u_id]})
            assert direct.status_code == 409, direct.text
            owners = (await client.get(f"/v1/harnesses/{harness_id}/access")).json()["owners"]
            shared = await client.put(
                f"/v1/harnesses/{harness_id}/access", json={"owners": [o["id"] for o in owners], "grants": {u_id: "read"}},
            )
            assert shared.status_code == 200, shared.text
            for agent_id in agent_ids:
                assert (await u.get(f"/v1/agents/{agent_id}")).status_code == 200
    finally:
        await client.delete(f"/v1/harnesses/{harness_id}")
