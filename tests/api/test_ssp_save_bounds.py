"""T126 (GI-22): the REST save door of a semantic-search provider refuses the HNSW settings pgvector refuses."""

from __future__ import annotations


async def test_the_rest_save_door_answers_422_for_an_out_of_range_setting(client):
    body = {"id": "ssp-bad", "provider": "pgvector",
            "config": {"hostname": "h", "username": "u", "password": "p", "database": "d", "hnsw_ef_search": 5000}}
    created = await client.post("/v1/ssp", json=body)
    assert created.status_code == 422, created.text
    assert "hnsw_ef_search" in created.text
    ok = await client.post("/v1/ssp", json={**body, "id": "ssp-ok", "config": {**body["config"], "hnsw_ef_search": 200}})
    assert ok.status_code == 201, ok.text
    bad_update = await client.put("/v1/ssp/ssp-ok", json={**body, "id": "ssp-ok", "config": {**body["config"], "hnsw_m": 500}})
    assert bad_update.status_code == 422, bad_update.text
