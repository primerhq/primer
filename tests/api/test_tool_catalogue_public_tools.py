"""The REST tool catalogues show a person the public tools of toolsets they cannot read (access-control P4 Task 2).

``GET /v1/tools/catalogue`` and ``GET /v1/tools`` list the rows the person can see plus the rows with public tools (``tools:public``); a row the
person holds no level on shows ONLY its public tools, and a row with no public tool stays out. Admins see everything.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from primer.api.errors import register_error_handlers
from primer.api.routers import providers, tools
from primer.model.access import Access
from primer.model.chat import Tool
from primer.model.provider import SqliteConfig, Toolset, ToolsetProviderType
from primer.storage.sqlite import SqliteStorageProvider
from tests.api.test_workspace_access_routes import _ActorMiddleware, _as

_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


class _Provider:
    def __init__(self, toolset_id: str) -> None:
        self._id = toolset_id

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        for name in ("t1", "t2"):
            yield Tool(id=name, description=f"the tool {name}", toolset_id=self._id, args_schema=_SCHEMA)


class _Registry:
    def __init__(self, sp: Any) -> None:
        self.storage_provider = sp

    async def get_toolset(self, toolset_id: str):
        if toolset_id in {"private", "shared", "one-tool"}:
            return _Provider(toolset_id)
        raise KeyError(toolset_id)  # a built-in the fake does not run: skipped by the catalogue


@pytest.fixture
async def client(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    for tid, access in {
        "private": Access(owners=["bob"]),
        "shared": Access(owners=["bob"], public=True),
        "one-tool": Access(owners=["bob"], public_tools=["t1"]),
    }.items():
        await sp.get_storage(Toolset).create(Toolset(id=tid, provider=ToolsetProviderType.INTERNAL, access=access))
    app = FastAPI()
    register_error_handlers(app)
    app.state.storage_provider = sp
    app.state.provider_registry = _Registry(sp)
    app.state.mcp_oauth = object()  # the INTERNAL rows below have no OAuth target, so it is never asked
    app.add_middleware(_ActorMiddleware)
    app.include_router(tools.tools_router, prefix="/v1")
    app.include_router(providers.builtin_toolsets_router, prefix="/v1")
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await sp.aclose()


@pytest.mark.parametrize(("who", "role", "expected"), [
    ("alice", "user", ["one-tool__t1", "shared__t1", "shared__t2"]),
    ("bob", "user", ["one-tool__t1", "one-tool__t2", "private__t1", "private__t2", "shared__t1", "shared__t2"]),
    ("root", "admin", ["one-tool__t1", "one-tool__t2", "private__t1", "private__t2", "shared__t1", "shared__t2"]),
])
async def test_the_flat_catalogue_shows_public_tools_of_unreadable_rows(client, who, role, expected) -> None:
    body = (await client.get("/v1/tools/catalogue", headers=_as(who, role))).json()
    assert sorted(i["id"] for i in body["items"] if i["id"].split("__")[0] in {"private", "shared", "one-tool"}) == expected


@pytest.mark.parametrize(("who", "role", "expected"), [
    ("alice", "user", {"one-tool": ["t1"], "shared": ["t1", "t2"]}),
    ("bob", "user", {"one-tool": ["t1", "t2"], "private": ["t1", "t2"], "shared": ["t1", "t2"]}),
    ("root", "admin", {"one-tool": ["t1", "t2"], "private": ["t1", "t2"], "shared": ["t1", "t2"]}),
])
async def test_the_grouped_catalogue_shows_public_tools_of_unreadable_rows(client, who, role, expected) -> None:
    response = await client.get("/v1/tools", headers=_as(who, role))
    assert response.status_code == 200, response.text
    body = response.json()
    got = {e["id"]: sorted(t["id"] for t in e["tools"]) for e in body["items"] if not e["builtin"]}
    assert got == expected
