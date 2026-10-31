"""Two users and their workspaces: private defaults, read then full sharing, the steered turn's person, revoke (access control, Phase 2)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

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
        # A user's default workspace is never deletable while the user records it; once the user is gone the admin removes it.
        with contextlib.suppress(Exception):
            await client.delete(f"/v1/workspaces/ws-{user_id}")


async def _default_workspace(c: httpx.AsyncClient, user_id: str) -> str:
    status = await c.get("/v1/auth/status")
    assert status.status_code == 200, status.text
    wid = status.json()["default_workspace_id"]
    assert wid == f"ws-{user_id}", status.text
    return wid


async def _share(owner: httpx.AsyncClient, wid: str, owner_id: str, grants: dict[str, str]) -> None:
    access = await owner.get(f"/v1/workspaces/{wid}/access")
    assert access.status_code == 200, access.text
    put = await owner.put(f"/v1/workspaces/{wid}/access", json={"owners": [owner_id], "grants": grants, "rev": access.json()["rev"]})
    assert put.status_code == 200, put.text


def _denied(resp: httpx.Response) -> None:
    assert resp.status_code == 403, resp.text
    assert resp.json()["extensions"]["code"] == "access_denied", resp.text


async def test_a_workspace_is_private_until_shared_and_a_steered_turn_runs_as_the_peer(client, base_url, unique_suffix) -> None:
    async with (
        _user(client, base_url, f"acl2-a-{unique_suffix}") as (a_id, a),
        _user(client, base_url, f"acl2-b-{unique_suffix}") as (b_id, b),
    ):
        # Each full user gets a private default workspace at first sign-in; neither sees the other's.
        a_ws = await _default_workspace(a, a_id)
        b_ws = await _default_workspace(b, b_id)
        got = await a.get(f"/v1/workspaces/{a_ws}")
        assert got.status_code == 200, got.text
        assert got.json()["access"]["owners"] == [a_id] and got.json()["access"]["public"] is False
        assert (await b.get(f"/v1/workspaces/{a_ws}")).status_code == 404
        assert (await a.get(f"/v1/workspaces/{b_ws}")).status_code == 404
        listed = await b.get("/v1/workspaces", params={"limit": 200})
        assert a_ws not in [w["id"] for w in listed.json()["items"]]
        assert (await b.get("/v1/events", params={"workspace_id": a_ws})).status_code == 404

        # The owner's operator session (made at sign-in) and a file to read.
        main = await a.post(f"/v1/workspaces/{a_ws}/main_session")
        assert main.status_code in (200, 201), main.text
        sid = main.json()["id"]
        wrote = await a.put(f"/v1/workspaces/{a_ws}/files", params={"path": "acl2-notes.txt"}, json={"content": "shared notes"})
        assert wrote.status_code == 204, wrote.text

        # Read: the peer browses files and views sessions, but cannot steer, write or toggle the terminal.
        await _share(a, a_ws, a_id, {b_id: "read"})
        assert (await b.get(f"/v1/workspaces/{a_ws}")).status_code == 200
        assert (await b.get(f"/v1/workspaces/{a_ws}/files", params={"path": "."})).status_code == 200
        read = await b.get(f"/v1/workspaces/{a_ws}/files/read", params={"path": "acl2-notes.txt"})
        assert read.status_code == 200, read.text
        assert read.json()["content"] == "shared notes"
        assert (await b.get(f"/v1/workspaces/{a_ws}/sessions")).status_code == 200
        assert (await b.get(f"/v1/sessions/{sid}")).status_code == 200
        assert (await b.get("/v1/events", params={"workspace_id": a_ws})).status_code == 200
        _denied(await b.post(f"/v1/workspaces/{a_ws}/sessions/{sid}/steer", json={"instruction": "hello from b"}))
        _denied(await b.put(f"/v1/workspaces/{a_ws}/files", params={"path": "b.txt"}, json={"content": "x"}))
        _denied(await b.put(f"/v1/workspaces/{a_ws}/terminal_access", json={"enabled": True}))

        # Full: the peer steers the owner's session, and that turn runs as the peer; the session's creator is unchanged.
        await _share(a, a_ws, a_id, {b_id: "full"})
        steered = await b.post(f"/v1/workspaces/{a_ws}/sessions/{sid}/steer", json={"instruction": "hello from b"})
        assert steered.status_code == 200, steered.text
        assert steered.json()["turn_person"]["user_id"] == b_id, steered.text
        row = await a.get(f"/v1/sessions/{sid}")
        assert row.status_code == 200, row.text
        assert row.json()["turn_person"]["user_id"] == b_id
        assert row.json()["initiated_by"]["user_id"] == a_id

        # Revoke: the workspace, its sessions and its events answer like missing rows.
        await _share(a, a_ws, a_id, {})
        assert (await b.get(f"/v1/workspaces/{a_ws}")).status_code == 404
        assert (await b.get(f"/v1/workspaces/{a_ws}/sessions")).status_code == 404
        assert (await b.get(f"/v1/sessions/{sid}")).status_code == 404
        assert (await b.get("/v1/events", params={"workspace_id": a_ws})).status_code == 404
