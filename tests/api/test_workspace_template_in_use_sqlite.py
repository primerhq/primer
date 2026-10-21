"""Template deletion with a snapshotted workspace on real SQLite storage."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import SecretStr

from primer.model.workspace import (
    Workspace,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
)
from tests.api.test_provider_url_userinfo_sqlite import client, sp  # noqa: F401


@pytest.mark.asyncio
async def test_delete_keeps_the_workspace_snapshot(client, sp) -> None:
    template = WorkspaceTemplate(
        id="tpl-a", provider_id="p-loc", description="create-time recipe",
    )
    await sp.get_storage(WorkspaceTemplate).create(template)
    await sp.get_storage(Workspace).create(
        Workspace(
            id="ws-a",
            template_id=template.id,
            template_snapshot=template,
            provider_id=template.provider_id,
            created_at=datetime.now(timezone.utc),
            phase="running",
            runtime_meta=WorkspaceRuntimeMeta(
                url="ws://127.0.0.1:5959/", token=SecretStr("t"),
            ),
        )
    )

    response = await client.delete("/v1/workspace_templates/tpl-a")

    assert response.status_code == 204, response.text
    assert await sp.get_storage(WorkspaceTemplate).get("tpl-a") is None
    row = await sp.get_storage(Workspace).get("ws-a")
    assert row is not None and row.template_snapshot is not None
    assert row.template_snapshot.description == "create-time recipe"
