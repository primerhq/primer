"""``DELETE /v1/workspace_templates/{id}`` over REST with a REAL SQLite store (lead ruling 6 of plan 08, platform revamp 2026-10).

The in-memory store of ``tests/api/test_workspace_template_in_use.py`` answers in insertion order and ignores ``order_by``, so it cannot show that
the five ids the 409 names are the first five BY ID, or that the count is the store's own total. Here the app runs on ``SqliteStorageProvider``,
as in ``tests/api/test_workspace_template_url_source_sqlite.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import SecretStr

from primer.model.workspace import Workspace, WorkspaceRuntimeMeta, WorkspaceTemplate
from tests.api.test_provider_url_userinfo_sqlite import client, sp  # noqa: F401  (fixtures: an app on a real SQLite store)


def _workspace(workspace_id: str, template_id: str) -> Workspace:
    return Workspace(
        id=workspace_id, template_id=template_id, provider_id="p-loc", created_at=datetime.now(timezone.utc), phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959/", token=SecretStr("t")),
    )


@pytest.mark.asyncio
async def test_the_409_names_the_stores_count_and_the_first_five_ids(client, sp) -> None:
    await sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(id="tpl-a", provider_id="p-loc", description="d"))
    for n in (7, 3, 5, 1, 8, 2, 6, 4, 10, 9):
        await sp.get_storage(Workspace).create(_workspace(f"ws-{n}", "tpl-a"))
    await sp.get_storage(Workspace).create(_workspace("ws-other", "tpl-b"))

    r = await client.delete("/v1/workspace_templates/tpl-a")

    assert r.status_code == 409, r.text
    ext = r.json()["extensions"]
    assert ext["error"] == "template_in_use" and ext["count"] == 10, ext
    assert ext["workspace_ids"] == ["ws-1", "ws-10", "ws-2", "ws-3", "ws-4"], "the first five by id, in the store's (text) order"
    assert "10 workspaces" in r.json()["detail"] and "first 5: ws-1, ws-10, ws-2, ws-3, ws-4" in r.json()["detail"]
    assert await sp.get_storage(WorkspaceTemplate).get("tpl-a") is not None, "the refused delete took the template away"


@pytest.mark.asyncio
async def test_the_template_deletes_when_the_last_workspace_made_from_it_is_gone(client, sp) -> None:
    await sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(id="tpl-a", provider_id="p-loc", description="d"))
    for workspace_id in ("ws-a", "ws-b"):
        await sp.get_storage(Workspace).create(_workspace(workspace_id, "tpl-a"))

    assert (await client.delete("/v1/workspace_templates/tpl-a")).status_code == 409
    await sp.get_storage(Workspace).delete("ws-a")
    still = await client.delete("/v1/workspace_templates/tpl-a")
    assert still.status_code == 409 and still.json()["extensions"]["workspace_ids"] == ["ws-b"], still.text
    await sp.get_storage(Workspace).delete("ws-b")

    gone = await client.delete("/v1/workspace_templates/tpl-a")

    assert gone.status_code == 204, gone.text
    assert await sp.get_storage(WorkspaceTemplate).get("tpl-a") is None
