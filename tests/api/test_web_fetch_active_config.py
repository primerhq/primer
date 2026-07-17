import pytest


@pytest.mark.asyncio
async def test_get_default_is_single_local(client):
    r = await client.get("/v1/web_fetch_active_config")
    assert r.status_code == 200
    assert r.json()["config"]["provider_id"] == "local"


@pytest.mark.asyncio
async def test_put_unknown_provider_rejected(client):
    r = await client.put("/v1/web_fetch_active_config", json={
        "config": {"mode": "single", "provider_id": "nope"}
    })
    assert r.status_code == 422
    assert "nope" in r.json()["extensions"]["unknown_ids"]


@pytest.mark.asyncio
async def test_put_aggregated_then_cascade_block(client):
    await client.post("/v1/web_fetch_providers", json={
        "id": "fc", "provider_type": "firecrawl",
        "config": {"type": "firecrawl", "api_key": "fc-x"},
    })
    r = await client.put("/v1/web_fetch_active_config", json={
        "config": {"mode": "aggregated", "provider_ids": ["local", "fc"]}
    })
    assert r.status_code == 200
    d = await client.delete("/v1/web_fetch_providers/fc")
    assert d.status_code == 409
    assert d.json()["extensions"]["error"] == "cascade_blocked"
