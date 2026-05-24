"""HTTP-surface tests for /v1/ssp CRUD."""

from __future__ import annotations

import httpx
import pytest


def _ssp_body(entity_id: str) -> dict:
    return {
        "id": entity_id,
        "provider": "pgvector",
        "config": {
            "hostname": "localhost",
            "port": 5432,
            "database": "matrix",
            "username": "matrix",
            "password": "matrix",
            "db_schema": "public",
        },
    }


@pytest.mark.asyncio
async def test_ssp_crud_round_trip_redacts_secrets(client):
    body = _ssp_body("ssp-rt")
    r = await client.post("/v1/ssp", json=body)
    assert r.status_code == 201, r.text
    try:
        r = await client.get("/v1/ssp/ssp-rt")
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["id"] == "ssp-rt"
        assert got["provider"] == "pgvector"
        # SecretStr's __str__ on dump is "**********"
        assert got["config"]["password"] == "**********", got
    finally:
        r = await client.delete("/v1/ssp/ssp-rt")
        assert r.status_code in (200, 204), r.text


@pytest.mark.asyncio
async def test_ssp_list_envelope_shape(client):
    r = await client.get("/v1/ssp?length=5")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.get("items"), list), body
    assert body.get("kind") == "offset", body


@pytest.mark.asyncio
async def test_ssp_invalidate_returns_204(client):
    body = _ssp_body("ssp-inv")
    r = await client.post("/v1/ssp", json=body)
    assert r.status_code == 201, r.text
    try:
        r = await client.post("/v1/ssp/ssp-inv/invalidate")
        assert r.status_code == 204, r.text
    finally:
        await client.delete("/v1/ssp/ssp-inv")
