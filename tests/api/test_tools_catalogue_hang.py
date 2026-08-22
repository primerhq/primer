"""A hanging toolset must not stall GET /v1/tools/catalogue.

Found on the live primer.ws.local deployment 2026-08-17: the endpoint
never returned (45s, zero bytes) while /v1/agents answered in 26ms. The
handler already skips toolsets that RAISE, but an unreachable MCP server
does not raise, it blocks. Because each toolset is enumerated with a
sequential ``await``, one hanging provider stalls the whole catalogue
forever and every picker built on it.
"""

from __future__ import annotations

import asyncio

import pytest

from primer.api.registries.provider_registry import ProviderRegistry
from primer.api.routers import tools as tools_router_mod

_HANGING_ID = "system"


class _HangingToolset:
    """Stands in for an MCP server that accepts the connection and
    then never answers."""

    async def list_tools(self, *, principal=None):
        await asyncio.Event().wait()  # never set
        yield  # pragma: no cover -- unreachable


@pytest.mark.asyncio
async def test_hanging_toolset_does_not_stall_the_catalogue(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(tools_router_mod, "_ENUMERATE_TIMEOUT_S", 0.2)

    original = ProviderRegistry.get_toolset

    async def _patched(self, toolset_id, *args, **kwargs):
        if toolset_id == _HANGING_ID:
            return _HangingToolset()
        return await original(self, toolset_id, *args, **kwargs)

    monkeypatch.setattr(ProviderRegistry, "get_toolset", _patched)

    resp = await asyncio.wait_for(
        client.get("/v1/tools/catalogue"), timeout=15.0
    )

    assert resp.status_code == 200, resp.text
    ids = [item["id"] for item in resp.json()["items"]]
    # The hanging toolset contributes nothing...
    assert not [i for i in ids if i.startswith(f"{_HANGING_ID}__")], (
        "the hanging toolset should have been skipped"
    )
    # ...but the healthy ones still do, which is the whole point: one
    # broken provider must not blank the picker.
    assert ids, "healthy toolsets should still be enumerated"
