"""PERF-T10 (M9) characterization: what the tool catalogues answer today, pinned before the per-row work becomes concurrent.

``GET /v1/tools/catalogue`` (flat) and ``GET /v1/tools`` (grouped) keep their observable contract: built-ins first in the one list's order, user rows in storage order, a toolset whose provider the registry cannot run is skipped (flat) or reported ``available: false`` with a reason (grouped) without blanking the rest of the catalogue, and a row the caller holds no level on shows only its public tools with no auth block.

These tests must pass on the unchanged code and stay green after the routes gather the per-row work.
"""

from __future__ import annotations

import asyncio
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


class _ProbeProvider:
    """A toolset provider whose enumeration yields two tools (provider order: t1 then t2)."""

    def __init__(self, toolset_id: str) -> None:
        self._id = toolset_id

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        for name in ("t1", "t2"):
            yield Tool(id=name, description=f"the tool {name}", toolset_id=self._id, args_schema=_SCHEMA)


class _Registry:
    """The fake provider registry: user toolsets resolve, everything else (built-ins, broken rows) does not."""

    def __init__(self, toolset_ids: set[str]) -> None:
        self._ids = toolset_ids

    async def get_toolset(self, toolset_id: str):
        if toolset_id in self._ids:
            return _ProbeProvider(toolset_id)
        raise KeyError(toolset_id)  # a built-in the fake does not run, a broken row


async def _build_app(sp, registry: _Registry, oauth: Any = object()) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.state.storage_provider = sp
    app.state.provider_registry = registry
    app.state.mcp_oauth = oauth
    app.add_middleware(_ActorMiddleware)
    app.include_router(tools.tools_router, prefix="/v1")
    app.include_router(providers.builtin_toolsets_router, prefix="/v1")
    return app


async def _seeded_sp(tmp_path: Path) -> SqliteStorageProvider:
    """ts-alpha: bob owns it, one public tool (alice sees t1 only). ts-beta: bob owns it. ts-broken: the registry cannot run it."""
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    await sp.get_storage(Toolset).create(Toolset(id="ts-alpha", provider=ToolsetProviderType.INTERNAL, access=Access(owners=["bob"], public_tools=["t1"])))
    await sp.get_storage(Toolset).create(Toolset(id="ts-beta", provider=ToolsetProviderType.INTERNAL, access=Access(owners=["bob"])))
    await sp.get_storage(Toolset).create(Toolset(id="ts-broken", provider=ToolsetProviderType.INTERNAL, access=Access(owners=["bob"])))
    return sp


@pytest.fixture
async def client(tmp_path: Path):
    sp = await _seeded_sp(tmp_path)
    app = await _build_app(sp, _Registry({"ts-alpha", "ts-beta"}))
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        await sp.aclose()


# ---- flat catalogue ------------------------------------------------------------


@pytest.mark.parametrize(("who", "role", "expected"), [
    ("alice", "user", {"ts-alpha__t1"}),
    ("bob", "user", {"ts-alpha__t1", "ts-alpha__t2", "ts-beta__t1", "ts-beta__t2"}),
    ("root", "admin", {"ts-alpha__t1", "ts-alpha__t2", "ts-beta__t1", "ts-beta__t2"}),
])
async def test_the_flat_catalogue_keeps_its_content(client, who, role, expected) -> None:
    body = (await client.get("/v1/tools/catalogue", headers=_as(who, role))).json()
    user_items = [i for i in body["items"] if i["id"].split("__")[0] in {"ts-alpha", "ts-beta", "ts-broken"}]
    assert {i["id"] for i in user_items} == expected
    # ts-broken is skipped, not an error: no entry, no crash, the rest answers.
    assert not any(i["id"].startswith("ts-broken") for i in user_items)


async def test_the_flat_catalogue_keeps_the_tools_in_provider_order(client) -> None:
    body = (await client.get("/v1/tools/catalogue", headers=_as("root", "admin"))).json()
    order = [i["id"] for i in body["items"]]
    assert order.index("ts-alpha__t1") < order.index("ts-alpha__t2")
    assert order.index("ts-beta__t1") < order.index("ts-beta__t2")


# ---- grouped catalogue ---------------------------------------------------------


async def test_the_grouped_catalogue_keeps_its_content_and_reports_broken_rows(client) -> None:
    response = await client.get("/v1/tools", headers=_as("root", "admin"))
    assert response.status_code == 200, response.text
    body = response.json()
    entries = {e["id"]: e for e in body["items"]}
    for tid in ("ts-alpha", "ts-beta"):
        assert entries[tid]["available"] is True
        assert [t["id"] for t in entries[tid]["tools"]] == ["t1", "t2"]
        assert "auth" not in entries[tid]          # no OAuth target: no auth block
    broken = entries["ts-broken"]
    assert broken["available"] is False
    assert "KeyError" in broken["unavailable_reason"]
    assert broken["tools"] == []


async def test_the_grouped_catalogue_keeps_the_builtins_in_the_one_lists_order(client) -> None:
    body = (await client.get("/v1/tools", headers=_as("root", "admin"))).json()
    builtin_ids = [e["id"] for e in body["items"] if e.get("builtin")]
    expected = [s["id"] for s in providers._BUILTIN_TOOLSETS if s["id"] != "canvas"]
    # canvas may be off in a bare app; its position in the one list is fixed either way.
    assert builtin_ids == expected or builtin_ids == expected + ["canvas"]


async def test_the_grouped_catalogue_shows_only_public_tools_of_a_row_the_caller_cannot_read(client) -> None:
    body = (await client.get("/v1/tools", headers=_as("alice", "user"))).json()
    entries = {e["id"]: e for e in body["items"] if not e.get("builtin")}
    # A row alice holds no level on stays out entirely unless it has a public tool.
    assert set(entries) == {"ts-alpha"}
    assert [t["id"] for t in entries["ts-alpha"]["tools"]] == ["t1"]   # the public one only
    assert "auth" not in entries["ts-alpha"]
