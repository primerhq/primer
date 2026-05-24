"""E2E: bring the API up against a SQLite storage backend and walk a
short user journey — LLM provider create → list → delete.

Pins that every router that uses ``app.state.storage_provider`` works
when the backend is SQLite. If a router accidentally depends on a
Postgres-only feature (e.g. ``LISTEN/NOTIFY``), this test surfaces it
loudly.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from matrix.api.app import create_app
from matrix.api.config import AppConfig
from matrix.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from matrix.model.scheduler import RuntimeMode


@pytest.mark.asyncio
async def test_llm_provider_crud_against_sqlite_backend(tmp_path: Path) -> None:
    cfg = AppConfig(
        runtime_mode=RuntimeMode.API,  # no worker; keeps the test fast
        db=StorageProviderConfig(
            provider=StorageProviderType.SQLITE,
            config=SqliteConfig(path=tmp_path / "e2e.sqlite"),
        ),
    )
    fastapi_app = create_app(cfg)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fastapi_app),
        base_url="http://test",
    ) as client:
        async with fastapi_app.router.lifespan_context(fastapi_app):
            # 1. List is empty.
            r = await client.get("/v1/llm_providers")
            assert r.status_code == 200, r.text
            assert r.json().get("items") == []

            # 2. Create one.
            body = {
                "id": "sqlite-e2e-llm",
                "provider": "openresponses",
                "models": [{"name": "m", "context_length": 4096}],
                "config": {
                    "url": "http://localhost:1",
                    "api_key": "k",
                    "flavor": "other",
                },
                "limits": {"max_concurrency": 1},
            }
            r = await client.post("/v1/llm_providers", json=body)
            assert r.status_code == 201, r.text

            # 3. Get it back.
            r = await client.get("/v1/llm_providers/sqlite-e2e-llm")
            assert r.status_code == 200, r.text
            assert r.json()["id"] == "sqlite-e2e-llm"

            # 4. List shows it.
            r = await client.get("/v1/llm_providers")
            assert r.status_code == 200, r.text
            ids = [x["id"] for x in r.json().get("items", [])]
            assert "sqlite-e2e-llm" in ids

            # 5. Delete it.
            r = await client.delete("/v1/llm_providers/sqlite-e2e-llm")
            assert r.status_code in (200, 204), r.text

    # The SQLite file should be present and non-empty after the journey.
    db_file = tmp_path / "e2e.sqlite"
    assert db_file.is_file()
    assert db_file.stat().st_size > 0
