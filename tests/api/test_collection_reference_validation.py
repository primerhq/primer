"""POST/PUT Collection: reference + immutability validation."""

from __future__ import annotations

import pytest


def _ssp_body(sid: str) -> dict:
    return {
        "id": sid,
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
async def test_post_collection_unknown_ssp_returns_404(client):
    r = await client.post(
        "/v1/collections",
        json={
            "id": "c-ref-1",
            "description": "unknown ssp",
            "embedder": {"provider_id": "emb-stub", "model": "stub"},
            "search_provider_id": "ssp-does-not-exist",
        },
    )
    assert r.status_code == 404, r.text
    body = r.json()
    assert body.get("type") == "/errors/not-found", body
    assert "ssp-does-not-exist" in body.get("detail", ""), body


@pytest.mark.asyncio
async def test_put_collection_changing_ssp_returns_422(client):
    # Seed two SSPs + a collection bound to the first.
    for sid in ("ssp-imm-a", "ssp-imm-b"):
        r = await client.post("/v1/ssp", json=_ssp_body(sid))
        assert r.status_code == 201, r.text
    try:
        r = await client.post(
            "/v1/collections",
            json={
                "id": "c-imm",
                "description": "immutable test",
                "embedder": {"provider_id": "emb-stub", "model": "stub"},
                "search_provider_id": "ssp-imm-a",
            },
        )
        assert r.status_code == 201, r.text

        r = await client.put(
            "/v1/collections/c-imm",
            json={
                "id": "c-imm",
                "description": "immutable test",
                "embedder": {"provider_id": "emb-stub", "model": "stub"},
                "search_provider_id": "ssp-imm-b",  # changed!
            },
        )
        assert r.status_code == 422, r.text
        body = r.json()
        # HTTPException detail is nested under "detail" key in FastAPI's
        # default exception handler.
        inner = body.get("detail", body)
        errors = inner.get("extensions", {}).get("errors", [])
        assert any(
            "search_provider_id" in (e.get("loc") or [])
            for e in errors
        ), body
    finally:
        await client.delete("/v1/collections/c-imm")
        await client.delete("/v1/ssp/ssp-imm-a")
        await client.delete("/v1/ssp/ssp-imm-b")


@pytest.mark.asyncio
async def test_put_collection_same_ssp_succeeds(client):
    # Updating description without changing search_provider_id works.
    r = await client.post("/v1/ssp", json=_ssp_body("ssp-update-ok"))
    assert r.status_code == 201, r.text
    try:
        r = await client.post(
            "/v1/collections",
            json={
                "id": "c-update",
                "description": "before",
                "embedder": {"provider_id": "emb-stub", "model": "stub"},
                "search_provider_id": "ssp-update-ok",
            },
        )
        assert r.status_code == 201, r.text

        r = await client.put(
            "/v1/collections/c-update",
            json={
                "id": "c-update",
                "description": "after",  # only this changed
                "embedder": {"provider_id": "emb-stub", "model": "stub"},
                "search_provider_id": "ssp-update-ok",
            },
        )
        assert r.status_code in (200, 204), r.text
    finally:
        await client.delete("/v1/collections/c-update")
        await client.delete("/v1/ssp/ssp-update-ok")
