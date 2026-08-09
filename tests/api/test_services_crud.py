"""CRUD + guards for /v1/services (spec sections 4 and 8)."""

from __future__ import annotations

import pytest

from primer.model.service import Service


async def _mk(client, name="status-page", **kw):
    body = {"name": name, "description": "a test service"}
    body.update(kw)
    r = await client.post("/v1/services", json=body)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_create_defaults(client):
    body = await _mk(client)
    assert body["viewer_auth"] == "console"
    assert body["active_version_id"] is None
    assert body["harness_id"] is None


@pytest.mark.asyncio
async def test_bad_slug_422(client):
    r = await client.post(
        "/v1/services", json={"name": "Bad Name", "description": "x"}
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_reserved_slug_422(client):
    r = await client.post(
        "/v1/services", json={"name": "_client", "description": "x"}
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_get_and_list(client):
    made = await _mk(client, name="listable")
    got = await client.get(f"/v1/services/{made['id']}")
    assert got.status_code == 200
    assert got.json()["name"] == "listable"
    listing = await client.get("/v1/services")
    assert listing.status_code == 200
    assert any(s["name"] == "listable" for s in listing.json()["items"])


@pytest.mark.asyncio
async def test_rename_unpublished_ok_published_409(client, fake_storage_provider):
    body = await _mk(client, name="renameable")
    sid = body["id"]

    body["name"] = "renamed"
    r = await client.put(f"/v1/services/{sid}", json=body)
    assert r.status_code == 200, r.text

    # Simulate a published service by pointing the row at a version.
    storage = fake_storage_provider.get_storage(Service)
    row = await storage.get(sid)
    row.active_version_id = "service-version-fake"
    await storage.update(row)

    current = (await client.get(f"/v1/services/{sid}")).json()
    current["name"] = "renamed-again"
    r = await client.put(f"/v1/services/{sid}", json=current)
    assert r.status_code == 409, r.text

    # Same name, other fields editable while published.
    current["name"] = "renamed"
    current["description"] = "edited while published"
    r = await client.put(f"/v1/services/{sid}", json=current)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_delete_and_404_on_second_delete(client):
    body = await _mk(client, name="deleteme")
    r = await client.delete(f"/v1/services/{body['id']}")
    assert r.status_code in (200, 204)
    r = await client.delete(f"/v1/services/{body['id']}")
    assert r.status_code == 404
