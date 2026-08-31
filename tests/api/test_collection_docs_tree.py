"""v2 slug-path docs API: CRUD, move, recursive delete, grep, 403s."""
from __future__ import annotations


async def _mk_collection(client, description="wiki"):
    r = await client.post("/v1/collections", json={"description": description})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_create_read_roundtrip_with_children(client):
    cid = await _mk_collection(client)
    r = await client.post(f"/v1/collections/{cid}/docs",
                          json={"parent": "", "slug": "guides", "body": "# G"})
    assert r.status_code == 201, r.text
    r = await client.post(f"/v1/collections/{cid}/docs",
                          json={"parent": "guides", "slug": "intro", "body": "hi"})
    assert r.status_code == 201, r.text
    r = await client.get(f"/v1/collections/{cid}/docs", params={"path": "guides"})
    assert r.status_code == 200
    data = r.json()
    assert data["body"] == "# G"
    assert [c["path"] for c in data["children"]] == ["guides/intro"]


async def test_patch_updates_body_and_title(client):
    cid = await _mk_collection(client)
    await client.post(f"/v1/collections/{cid}/docs",
                      json={"parent": "", "slug": "a", "body": "v1"})
    r = await client.patch(f"/v1/collections/{cid}/docs", params={"path": "a"},
                           json={"body": "v2", "title": "Ay"})
    assert r.status_code == 200, r.text
    r = await client.get(f"/v1/collections/{cid}/docs", params={"path": "a"})
    assert r.json()["body"] == "v2"
    assert r.json()["document"]["title"] == "Ay"


async def test_listing_parent_depth(client):
    cid = await _mk_collection(client)
    await client.post(f"/v1/collections/{cid}/docs",
                      json={"parent": "", "slug": "a", "body": ""})
    await client.post(f"/v1/collections/{cid}/docs",
                      json={"parent": "a", "slug": "b", "body": ""})
    r = await client.get(f"/v1/collections/{cid}/docs",
                         params={"parent": "", "depth": 2})
    assert [n["path"] for n in r.json()["nodes"]] == ["a", "a/b"]


async def test_move_and_recursive_delete(client):
    cid = await _mk_collection(client)
    await client.post(f"/v1/collections/{cid}/docs",
                      json={"parent": "", "slug": "src", "body": ""})
    await client.post(f"/v1/collections/{cid}/docs",
                      json={"parent": "src", "slug": "kid", "body": "K"})
    await client.post(f"/v1/collections/{cid}/docs",
                      json={"parent": "", "slug": "dst", "body": ""})
    r = await client.post(f"/v1/collections/{cid}/docs/move",
                          json={"path": "src", "new_parent": "dst"})
    assert r.status_code == 200
    r = await client.get(f"/v1/collections/{cid}/docs",
                         params={"path": "dst/src/kid"})
    assert r.status_code == 200
    r = await client.delete(f"/v1/collections/{cid}/docs", params={"path": "dst"})
    assert r.status_code == 409
    r = await client.delete(f"/v1/collections/{cid}/docs",
                            params={"path": "dst", "recursive": "true"})
    assert r.status_code == 204


async def test_not_found_names_siblings(client):
    cid = await _mk_collection(client)
    await client.post(f"/v1/collections/{cid}/docs",
                      json={"parent": "", "slug": "real", "body": ""})
    r = await client.get(f"/v1/collections/{cid}/docs", params={"path": "fake"})
    assert r.status_code == 404 and "real" in r.text


async def test_grep_endpoint(client):
    cid = await _mk_collection(client)
    await client.post(f"/v1/collections/{cid}/docs",
                      json={"parent": "", "slug": "a", "body": "one\nneedle two"})
    r = await client.get(f"/v1/collections/{cid}/grep", params={"q": "needle"})
    hits = r.json()["hits"]
    assert hits == [{"path": "a", "line": 2, "excerpt": "needle two"}]
    assert r.json()["truncated"] is False


async def test_body_cap_rejected(client):
    cid = await _mk_collection(client)
    r = await client.post(
        f"/v1/collections/{cid}/docs",
        json={"parent": "", "slug": "big", "body": "x" * (1024 * 1024 + 1)},
    )
    assert r.status_code == 422


async def test_system_collection_writes_are_403(client):
    r = await client.post(
        "/v1/collections",
        json={"id": "collection-sys", "description": "sys", "system": True},
    )
    assert r.status_code == 201, r.text
    resp = await client.post(
        "/v1/collections/collection-sys/docs",
        json={"parent": "", "slug": "a", "body": "x"},
    )
    assert resp.status_code == 403
