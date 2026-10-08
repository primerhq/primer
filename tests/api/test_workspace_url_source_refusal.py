"""A url file source the SSRF guard refuses is a client error, not a 500 (CI e2e on #473, test_seed_url_file[local]).

The guard raised a bare ``RuntimeError`` from inside materialisation, so ``POST /v1/workspaces`` answered 500
``/errors/internal`` for what is the caller's own bad input. It is a 422 ``/errors/validation-error`` problem now, and
its detail names the refused host.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from primer.model.storage import OffsetPage
from primer.model.workspace import Workspace
from tests.api.conftest import app, fake_provider_registry  # noqa: F401


@pytest.fixture
async def admin(app) -> AsyncIterator[httpx.AsyncClient]:  # noqa: F811
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/auth/register", json={"username": "urladmin", "password": "urladminpass1"})
        assert r.status_code == 200, r.text
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://127.0.0.1:1/seed-content", "http://169.254.169.254/latest/meta-data/"])
async def test_a_refused_url_source_is_a_422_naming_the_host(app, admin, tmp_path: Path, url) -> None:  # noqa: F811
    r = await admin.post("/v1/workspace_providers", json={
        "id": "p-loc", "provider": "local", "config": {"kind": "local", "root_path": str(tmp_path)},
    })
    assert r.status_code == 201, r.text
    r = await admin.post("/v1/workspace_templates", json={
        "id": "tpl-url", "provider_id": "p-loc", "description": "d",
        "files": [{"path": "seed.txt", "source": {"kind": "url", "url": url}}],
    })
    assert r.status_code == 201, r.text

    r = await admin.post("/v1/workspaces", json={"template_id": "tpl-url"})

    assert r.status_code == 422, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "/errors/validation-error", body
    host = url.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0]
    assert host in body["detail"] and "refused" in body["detail"], body
    page = await app.state.storage_provider.get_storage(Workspace).list(OffsetPage(offset=0, length=10))
    assert page.items == []
