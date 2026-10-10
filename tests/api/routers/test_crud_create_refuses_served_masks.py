"""``make_crud_router`` refuses a create whose body carries a mask a GET serves, on the plain route and on the scoped one (ticket 01a1212a, part B1).

A create has nothing stored to restore a mask from, so the literal mask would be stored as the value: the copy-a-row move (GET a row, POST it under a new id) stored the URL's ``**********`` password and the
key's ``**********abcd``. Both ``POST`` variants of the router call ``refuse_served_masks`` before the create hooks; the providers are pinned over a real SQLite store in
``tests/api/test_masked_secret_origin_over_rest.py``, this file uses a small entity so the two code paths are reached directly (no router passes ``scope_field`` today, so the scoped one has no other test).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import AnyUrl, SecretStr

from primer.api.errors import register_error_handlers
from primer.api.routers._crud import make_crud_router
from primer.model.common import Identifiable


class _Row(Identifiable):
    workspace_id: str = "w1"
    token: SecretStr | None = None
    endpoint: AnyUrl | None = None


class _Store:
    def __init__(self) -> None:
        self.rows: dict[str, _Row] = {}

    async def get(self, id: str) -> _Row | None:
        return self.rows.get(id)

    async def create(self, entity: _Row) -> _Row:
        self.rows[entity.id] = entity
        return entity


@pytest_asyncio.fixture(params=["plain", "scoped"])
async def api(request) -> AsyncIterator[tuple[httpx.AsyncClient, _Store, str]]:
    store = _Store()
    if request.param == "plain":
        router = make_crud_router(model_cls=_Row, storage_dep=lambda: store, plural="rows", tag="rows")
        path = "/v1/rows"
    else:
        router = make_crud_router(
            model_cls=_Row, storage_dep=lambda: store, plural="rows", tag="rows", scope_field="workspace_id", parent_path_segment="workspaces",
        )
        path = "/v1/workspaces/w1/rows"
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router, prefix="/v1")
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, store, path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "wording"),
    [
        pytest.param({"token": "**********"}, "re-enter the secret", id="the bare mask"),
        pytest.param({"token": "**********cdef"}, "re-enter the secret", id="the mask with the last four characters"),
        pytest.param({"endpoint": "http://svc:**********@px.lan/v1"}, "re-enter the password", id="a URL with the password mask"),
    ],
)
async def test_a_create_that_carries_a_served_mask_is_a_422_and_stores_nothing(api, body: dict[str, Any], wording: str) -> None:
    client, store, path = api

    r = await client.post(path, json={"id": "row-1", **body})

    assert r.status_code == 422, r.text
    assert wording in r.text
    assert store.rows == {}


@pytest.mark.asyncio
async def test_a_create_with_real_values_still_works(api) -> None:
    client, store, path = api

    r = await client.post(path, json={"id": "row-2", "token": "a-real-secret-value", "endpoint": "http://svc:pw@px.lan/v1"})

    assert r.status_code == 201, r.text
    assert store.rows["row-2"].token.get_secret_value() == "a-real-secret-value"
    assert str(store.rows["row-2"].endpoint) == "http://svc:pw@px.lan/v1"
