"""PERF-T10 (M9) perf: the tool catalogues run the per-row work concurrently and keep storage reads bounded.

The content/order/visibility halves (what the catalogues answer) live in
``test_tool_catalogues_listing.py`` and stay green through the change. These perf halves are RED on the
serial implementation and green once the routes gather the per-row work under a bound:

* per-row enumeration (flat) and the per-row probe + OAuth status (grouped) overlap: a probe that
  holds every call open for 100 ms sees a peak of >= 2 concurrent calls (serial: 1);
* a catalogue of 100 toolsets, 50 of them OAuth rows, makes <= 3 Toolset storage queries (the 200-row
  page walk) instead of one read per row.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from primer.api.routers import providers
from primer.model.access import Access
from primer.model.chat import Tool
from primer.model.provider import HttpConfig, McpConfig, OAuthConfig, SqliteConfig, Toolset, ToolsetProviderType, TransportType
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.oauth.service import ToolsetAuthStatus
from tests.api.test_tool_catalogues_listing import _build_app
from tests.api.test_workspace_access_routes import _as
from tests.perf.counting import CountingStorageProvider

_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


class _Overlap:
    """A 100 ms hold shared by many calls: ``peak`` counts how many ran at once."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    async def hold(self) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.1)
        finally:
            self.active -= 1


class _ProbeProvider:
    """Yields two tools, holding the drain open on ``overlap`` while it does."""

    def __init__(self, toolset_id: str, overlap: _Overlap) -> None:
        self._id = toolset_id
        self._overlap = overlap

    async def list_tools(self, *, principal: str | None = None):
        await self._overlap.hold()
        for name in ("t1", "t2"):
            yield Tool(id=name, description=f"the tool {name}", toolset_id=self._id, args_schema=_SCHEMA)


class _Registry:
    """User toolsets resolve to their probe provider; anything else (built-ins) raises."""

    def __init__(self, toolset_ids: set[str], overlap: _Overlap | None) -> None:
        self._providers = {tid: _ProbeProvider(tid, overlap) for tid in toolset_ids}

    async def get_toolset(self, toolset_id: str):
        if toolset_id in self._providers:
            return self._providers[toolset_id]
        raise KeyError(toolset_id)


class _FakeOAuth:
    """The OAuth service the grouped catalogue asks per OAuth row: holds its probe while answering."""

    def __init__(self, overlap: _Overlap | None = None) -> None:
        self._overlap = overlap

    async def status(self, row: Any, person: Any) -> ToolsetAuthStatus:
        if self._overlap is not None:
            await self._overlap.hold()
        return ToolsetAuthStatus(toolset_id=row.id, mode="none", state="not_applicable")


def _oauth_toolset(tid: str) -> Toolset:
    """An http MCP toolset with an oauth block: ``OAuthTarget.from_toolset`` resolves for every person."""
    return Toolset(
        id=tid,
        provider=ToolsetProviderType.MCP,
        config=McpConfig(
            transport=TransportType.HTTP,
            config=HttpConfig(url="http://localhost:9999/mcp", oauth=OAuthConfig()),
        ),
        access=Access(owners=["bob"]),
    )


@pytest.fixture
async def client(tmp_path: Path):
    """The overlap probe and the app built over it (4 internal rows ts-0..ts-3)."""
    overlap = _Overlap()
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    for i in range(4):
        await sp.get_storage(Toolset).create(Toolset(id=f"ts-{i}", provider=ToolsetProviderType.INTERNAL, access=Access(owners=["bob"])))
    app = await _build_app(sp, _Registry({f"ts-{i}" for i in range(4)}, overlap), _FakeOAuth())
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.overlap = overlap  # type: ignore[attr-defined]
        yield c
    await sp.aclose()


async def test_the_flat_catalogue_enumerates_the_rows_concurrently(client) -> None:
    response = await client.get("/v1/tools/catalogue", headers=_as("root", "admin"))
    assert response.status_code == 200, response.text
    assert {i["id"] for i in response.json()["items"] if i["id"].startswith("ts-")} == {f"ts-{i}__t{j}" for i in range(4) for j in ("1", "2")}
    # Serial per-row enumeration runs one probe at a time (peak 1); the gathered path overlaps them.
    assert client.overlap.peak >= 2  # type: ignore[attr-defined]


async def test_the_grouped_catalogue_gathers_the_probes_and_the_oauth_status(tmp_path: Path) -> None:
    enum, oauth = _Overlap(), _Overlap()
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    for i in range(2):
        await sp.get_storage(Toolset).create(Toolset(id=f"ts-{i}", provider=ToolsetProviderType.INTERNAL, access=Access(owners=["bob"])))
    for i in range(2, 4):
        await sp.get_storage(Toolset).create(_oauth_toolset(f"ts-{i}"))
    registry = _Registry({f"ts-{i}" for i in range(4)}, enum)
    app = await _build_app(sp, registry, _FakeOAuth(oauth))
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get("/v1/tools", headers=_as("root", "admin"))
    await sp.aclose()
    assert response.status_code == 200, response.text
    entries = {e["id"]: e for e in response.json()["items"]}
    for i in range(2, 4):
        assert entries[f"ts-{i}"]["available"] is True
        assert "auth" in entries[f"ts-{i}"]  # an OAuth row the admin reads gets its sign-in block
    assert enum.peak >= 2
    assert oauth.peak >= 2


async def test_the_catalogue_of_100_toolsets_makes_bounded_toolset_queries(tmp_path: Path) -> None:
    """100 rows, 50 of them OAuth: the Toolset storage is read by the page walk, not row by row."""
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    for i in range(100):
        row = _oauth_toolset(f"ts-{i:03d}") if i % 2 == 0 else Toolset(id=f"ts-{i:03d}", provider=ToolsetProviderType.INTERNAL, access=Access(owners=["bob"]))
        await sp.get_storage(Toolset).create(row)
    csp = CountingStorageProvider(sp)
    app = await _build_app(sp, _Registry(set(), None), _FakeOAuth())
    app.state.storage_provider = csp
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        with csp.counted() as counts:
            response = await c.get("/v1/tools", headers=_as("root", "admin"))
    await sp.aclose()
    assert response.status_code == 200, response.text
    user_entries = [e for e in response.json()["items"] if not e.get("builtin")]
    assert len(user_entries) == 100
    assert counts.calls("find", model="Toolset") <= 3
    assert getattr(providers, "_CATALOGUE_CONCURRENCY", 1) >= 2  # the bound the gathered probes run under
