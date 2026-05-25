"""HTTP-surface tests for /v1/ssp CRUD with the lance backend."""

from __future__ import annotations

import pytest

pytest.importorskip("lancedb")  # type: ignore[arg-type]


def _lance_body(entity_id: str, path: str) -> dict:
    return {
        "id": entity_id,
        "provider": "lance",
        "config": {
            "path": path,
        },
    }


@pytest.mark.asyncio
async def test_ssp_lance_post_creates_201(client, tmp_path):
    body = _lance_body("ssp-l-1", str(tmp_path / "lance"))
    r = await client.post("/v1/ssp", json=body)
    assert r.status_code == 201, r.text
    got = r.json()
    assert got["id"] == "ssp-l-1"
    assert got["provider"] == "lance"
    assert got["config"]["path"] == str(tmp_path / "lance")
    # No secret fields → redaction layer is a no-op; no fake "**********" leaks.
    assert "password" not in got["config"]
    # Cleanup
    r = await client.delete("/v1/ssp/ssp-l-1")
    assert r.status_code in (200, 204), r.text


@pytest.mark.asyncio
async def test_ssp_lance_get_round_trip(client, tmp_path):
    path = str(tmp_path / "lance")
    await client.post("/v1/ssp", json=_lance_body("ssp-l-2", path))
    try:
        r = await client.get("/v1/ssp/ssp-l-2")
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["provider"] == "lance"
        assert got["config"]["path"] == path
        assert got["config"]["distance"] == "cosine"
        assert got["config"]["hnsw_m"] == 16
        assert got["config"]["index_min_rows"] == 1000
    finally:
        await client.delete("/v1/ssp/ssp-l-2")


@pytest.mark.asyncio
async def test_ssp_lance_invalidate_returns_2xx(client, tmp_path):
    await client.post("/v1/ssp", json=_lance_body("ssp-l-3", str(tmp_path / "lance")))
    try:
        r = await client.post("/v1/ssp/ssp-l-3/invalidate")
        assert r.status_code in (200, 202, 204), r.text
        # Row still present after invalidate (only the cached provider is dropped).
        r = await client.get("/v1/ssp/ssp-l-3")
        assert r.status_code == 200, r.text
    finally:
        await client.delete("/v1/ssp/ssp-l-3")


@pytest.mark.asyncio
async def test_ssp_lance_provider_mismatched_config_returns_422(client):
    # 'lance' provider with a pgvector-style config must 422.
    bad = {
        "id": "ssp-bad",
        "provider": "lance",
        "config": {
            "hostname": "x",
            "port": 5432,
            "username": "u",
            "password": "p",
            "database": "d",
        },
    }
    r = await client.post("/v1/ssp", json=bad)
    assert r.status_code == 422, r.text
