"""A stored Toolset row cannot take an id that the registry answers without storage (ADM-08 of the 2026-10-08 admin review).

``ProviderRegistry.get_toolset`` resolves the built-in providers (system, workspaces, misc, web, harness, trigger, workspace_ext,
collections, crud) BEFORE it reads storage, and the tool manager owns the scope ids (external, workspace, workspace_ext). A row
stored under any of those ids is a ghost: ``GET /v1/toolsets`` lists it, nothing ever runs it, and the console cannot show it.
The REST create hook refused only the scope ids (the built-ins were rebound away by a module-local alias), so the console's own
"New toolset" form, whose hint says "Internal toolsets (system, workspaces, misc, search, web) are runtime built-ins, they cannot be
created via this form", created one for ``system``.
"""

from __future__ import annotations

import pytest

from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS, RESERVED_TOOLSET_SCOPE_IDS

# Every id a stored row may not take: the built-in providers the registry resolves first plus the tool-manager scopes.
EVERY_RESERVED_ID = sorted(RESERVED_TOOLSET_IDS | RESERVED_TOOLSET_SCOPE_IDS)


async def _create(client, tid: str):
    return await client.post(
        "/v1/toolsets", json={"id": tid, "provider": "python", "config": {"source": "", "source_version": 1}},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tid", EVERY_RESERVED_ID)
async def test_a_toolset_row_cannot_take_a_reserved_id(client, tid) -> None:
    resp = await _create(client, tid)

    assert resp.status_code == 409, resp.text
    ext = resp.json()["extensions"]
    assert ext["error"] == "reserved_id" and ext["kind"] == "toolset"
    assert tid in ext["reserved"]
    assert tid in resp.json()["detail"], "the refusal names the id that was refused"


@pytest.mark.asyncio
@pytest.mark.parametrize("tid", EVERY_RESERVED_ID)
async def test_a_refused_reserved_id_stores_nothing(client, tid) -> None:
    await _create(client, tid)

    stored = [row["id"] for row in (await client.get("/v1/toolsets")).json()["items"]]
    assert tid not in stored


@pytest.mark.asyncio
async def test_an_ordinary_id_is_still_accepted(client) -> None:
    resp = await _create(client, "my-own-tools")

    assert resp.status_code == 201, resp.text
