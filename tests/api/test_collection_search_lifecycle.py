"""PUT/GET/DELETE /collections/{cid}/search lifecycle."""
from __future__ import annotations


def _cfg(embedder_id="emb-x", ssp_id="ssp-x"):
    return {
        "embedder": {"provider_id": embedder_id, "model": "m"},
        "vector_store_provider_id": ssp_id,
    }


async def test_enable_unknown_provider_409_with_hint(client):
    r = await client.post("/v1/collections", json={"description": "w"})
    cid = r.json()["id"]
    resp = await client.put(f"/v1/collections/{cid}/search", json=_cfg())
    assert resp.status_code == 409
    assert "emb-x" in resp.text and "register" in resp.text.lower()


async def test_disable_always_works_and_status_reports_disabled(client):
    r = await client.post("/v1/collections", json={"description": "w"})
    cid = r.json()["id"]
    resp = await client.delete(f"/v1/collections/{cid}/search")
    assert resp.status_code == 204
    s = await client.get(f"/v1/collections/{cid}/search")
    assert s.status_code == 200 and s.json()["state"] == "disabled"


async def test_query_surfaces_error_state_hint(client):
    # A collection whose search block is already in error state (provider
    # deleted after enable). POST /v1/collections accepts the full v2
    # entity body; provider validation happens only on the PUT lifecycle
    # route.
    r = await client.post("/v1/collections", json={
        "description": "w",
        "search": {
            "embedder": {"provider_id": "emb-x", "model": "m"},
            "vector_store_provider_id": "ssp-x",
            "state": "error",
            "error": "embedding provider 'emb-x' was deleted; re-register "
                     "it or disable search on this collection",
        },
    })
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    resp = await client.post(f"/v1/collections/{cid}/search", json={"query": "q"})
    assert resp.status_code == 503
    assert "emb-x" in resp.text
