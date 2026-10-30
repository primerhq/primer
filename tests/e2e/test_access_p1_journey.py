"""Two users and an agent: ownership, sharing, public and hand-over over the live API (access control, Phase 1)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

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


async def _profile_id(client: httpx.AsyncClient) -> str:
    profiles = await client.get("/v1/model_profiles")
    assert profiles.status_code == 200, profiles.text
    items = profiles.json()["items"]
    if not items:
        pytest.skip("no model profile on this server")
    return items[0]["id"]


async def test_an_agent_is_private_until_shared_and_public_once_published(client, base_url, unique_suffix) -> None:
    profile_id = await _profile_id(client)
    agent_id = f"acl-agent-{unique_suffix}"
    async with (
        _user(client, base_url, f"acl-b-{unique_suffix}") as (b_id, b),
        _user(client, base_url, f"acl-c-{unique_suffix}") as (c_id, c),
        _user(client, base_url, f"acl-d-{unique_suffix}") as (_d_id, d),
    ):
        made = await b.post(
            "/v1/agents",
            json={"id": agent_id, "description": "access journey agent", "model": {"profile_id": profile_id}},
        )
        assert made.status_code == 201, made.text
        assert made.json()["access"]["owners"] == [b_id]
        try:
            # The admin sees every row; another user does not, and the row answers like a missing one.
            assert (await client.get(f"/v1/agents/{agent_id}")).status_code == 200
            assert (await c.get(f"/v1/agents/{agent_id}")).status_code == 404
            listed = await c.get("/v1/agents", params={"limit": 200})
            assert agent_id not in [a["id"] for a in listed.json()["items"]]

            # Shared read with C: C reads, but cannot edit or change sharing.
            access = (await b.get(f"/v1/agents/{agent_id}/access")).json()
            shared = await b.put(
                f"/v1/agents/{agent_id}/access",
                json={"owners": [b_id], "grants": {c_id: "read"}, "rev": access["rev"]},
            )
            assert shared.status_code == 200, shared.text
            got = await c.get(f"/v1/agents/{agent_id}")
            assert got.status_code == 200
            edit = await c.put(f"/v1/agents/{agent_id}", json={**got.json(), "description": "edited by c"})
            assert edit.status_code == 403, edit.text
            assert edit.json()["extensions"]["code"] == "access_denied"
            assert (await c.put(f"/v1/agents/{agent_id}/access", json={"owners": [c_id]})).status_code == 403

            # Public: a user with no grant lists and reads it.
            assert (await d.get(f"/v1/agents/{agent_id}")).status_code == 404
            published = await b.put(
                f"/v1/agents/{agent_id}/access",
                json={"owners": [b_id], "grants": {c_id: "read"}, "public": True, "rev": shared.json()["rev"]},
            )
            assert published.status_code == 200, published.text
            listed = await d.get("/v1/agents", params={"limit": 200})
            assert agent_id in [a["id"] for a in listed.json()["items"]]

            # Hand-over: C owns it, B keeps read and can no longer change sharing.
            handed = await b.put(
                f"/v1/agents/{agent_id}/access",
                json={"owners": [c_id], "grants": {b_id: "read"}, "public": True, "rev": published.json()["rev"]},
            )
            assert handed.status_code == 200, handed.text
            assert handed.json()["your_level"] == "read"
            assert (await b.put(f"/v1/agents/{agent_id}/access", json={"owners": [b_id]})).status_code == 403
            assert (await c.delete(f"/v1/agents/{agent_id}")).status_code == 204
        finally:
            await client.delete(f"/v1/agents/{agent_id}")


async def test_users_can_be_looked_up_by_prefix(client, base_url, unique_suffix) -> None:
    async with _user(client, base_url, f"acl-look-{unique_suffix}") as (look_id, look):
        found = await look.get("/v1/users/lookup", params={"q": f"acl-look-{unique_suffix}"})
        assert found.status_code == 200, found.text
        assert found.json() == [{"id": look_id, "username": f"acl-look-{unique_suffix}"}]
