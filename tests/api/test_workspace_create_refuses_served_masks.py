"""``POST /v1/workspaces`` refuses an ``overrides.env`` value that is the mask a GET serves (ticket 01a1212a, round 3 of #711, nit N2).

``WorkspaceRow.overrides`` keeps the per-instantiation ``env`` (``dict[str, SecretStr]``) and ``GET /v1/workspaces/{id}`` serves it masked, so the copy-a-workspace move (read the row, change the id,
``POST`` it) sent ``**********`` back as the variable's value and the workspace started with the mask in its environment. It is a 422 now, before anything is materialised; a real value works as before.
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
        r = await c.post("/v1/auth/register", json={"username": "maskadmin", "password": "maskadminpass1"})
        assert r.status_code == 200, r.text
        yield c


async def _template(admin, tmp_path: Path) -> None:
    r = await admin.post("/v1/workspace_providers", json={"id": "p-loc", "provider": "local", "config": {"kind": "local", "root_path": str(tmp_path)}})
    assert r.status_code == 201, r.text
    r = await admin.post("/v1/workspace_templates", json={"id": "tpl-a", "provider_id": "p-loc", "description": "d"})
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("served", ["**********", "**********abcd"], ids=["the bare mask", "the mask with the last four characters"])
async def test_an_overrides_env_value_that_is_a_served_mask_is_a_422_and_creates_nothing(app, admin, tmp_path: Path, served: str) -> None:  # noqa: F811
    await _template(admin, tmp_path)

    r = await admin.post("/v1/workspaces", json={"template_id": "tpl-a", "overrides": {"env": {"API_TOKEN": served}}})

    assert r.status_code == 422, r.text
    assert "API_TOKEN" in r.text and "re-enter" in r.text
    page = await app.state.storage_provider.get_storage(Workspace).list(OffsetPage(offset=0, length=10))
    assert page.items == []
