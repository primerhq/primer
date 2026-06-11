import pytest


@pytest.mark.asyncio
async def test_create_redacts_api_key(client):
    r = await client.post("/v1/web_fetch_providers", json={
        "id": "fc-prod", "provider_type": "firecrawl",
        "config": {"type": "firecrawl", "api_key": "fc-secret"},
    })
    assert r.status_code in (200, 201)
    assert "fc-secret" not in r.text


@pytest.mark.asyncio
async def test_create_reserved_id_rejected(client):
    r = await client.post("/v1/web_fetch_providers", json={
        "id": "local", "provider_type": "local", "config": {"type": "local"},
    })
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_delete_reserved_rejected(client):
    r = await client.delete("/v1/web_fetch_providers/local")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_types_endpoint(client):
    r = await client.get("/v1/web_fetch_providers/_types")
    assert r.status_code == 200
    body = r.json()
    assert "local" in body and body["local"]["config_fields"] == []
    assert "firecrawl" in body
