"""The workspace list tools refuse a url file source's password as a sort or seek key (ticket 01a11d32, the B1 of the #721 review).

``list_workspace_templates`` takes ``order_by`` and ``cursor`` straight to storage, and so does ``list_workspaces``. A template's ``files`` and a workspace's ``overrides`` hold ``kind=url`` sources
whose password the tool results serve masked while the stored row keeps it, so a sort or a forged cursor on the field is a sort or seek oracle on the password. A USER run (the caller the template
tools are open to) gets ``type=validation-error`` and no row; a plain field still sorts. Against a real SQLite backend.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import SecretStr

from primer.api.registries import WorkspaceRegistry
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.workspace import (
    FileMount,
    Workspace,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
    _UrlSource,
)
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.workspaces import build_workspaces_toolset

_PASSWORD = "s3cr3t"
_OVERRIDE_PASSWORD = "ovpass99"
USER = ToolContext(
    tool_call_id="call-u", session_id="sess-u", workspace_id=None,
    initiated_by=PrincipalRef(type="user", id="user-u", display="user", role="user", source="local"),
)


def _forge_cursor(field: str, value) -> str:
    keys = [
        {"field": field, "value": value, "direction": "asc", "is_null": value is None},
        {"field": "id", "value": "", "direction": "asc", "is_null": False},
    ]
    payload = json.dumps({"keys": keys}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()


def _typ(result) -> str:
    return json.loads(result.output)["type"]


@pytest_asyncio.fixture
async def toolset(tmp_path: Path) -> AsyncIterator:
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "urlq.sqlite"))
    await sp.initialize()
    await sp.get_storage(WorkspaceTemplate).create(
        WorkspaceTemplate.model_validate({
            "id": "tpl-a", "provider_id": "p-loc", "description": "d",
            "files": [{"path": "seed.txt", "source": {"kind": "url", "url": f"https://reader:{_PASSWORD}@files.example.com/seed.txt"}}],
        })
    )
    await sp.get_storage(Workspace).create(
        Workspace(
            id="ws-one", template_id="tpl-a", provider_id="p-loc", created_at=datetime.now(timezone.utc),
            overrides=WorkspaceTemplateOverrides(files=[FileMount(path="extra.txt", source=_UrlSource(url=f"https://reader:{_OVERRIDE_PASSWORD}@files.example.com/extra.txt"))]),
            runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:1/", token=SecretStr("t")),
        )
    )
    yield build_workspaces_toolset(storage_provider=sp, workspace_registry=WorkspaceRegistry(sp))
    await sp.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_a_user_run_cannot_sort_the_templates_by_files(toolset, direction: str) -> None:
    result = await toolset.call(tool_name="list_workspace_templates", arguments={"order_by": [f"files:{direction}"]}, ctx=USER)
    assert result.is_error and _typ(result) == "validation-error", result.output
    assert _PASSWORD not in result.output and "tpl-a" not in result.output


@pytest.mark.asyncio
async def test_a_user_run_cannot_seek_the_templates_with_a_forged_cursor_on_files(toolset) -> None:
    for guess in ("a", "z"):
        result = await toolset.call(tool_name="list_workspace_templates", arguments={"cursor": _forge_cursor("files", guess)}, ctx=USER)
        assert result.is_error and _typ(result) == "validation-error", result.output
        assert _PASSWORD not in result.output and "tpl-a" not in result.output


@pytest.mark.asyncio
async def test_a_user_run_cannot_sort_the_workspaces_by_overrides(toolset) -> None:
    result = await toolset.call(tool_name="list_workspaces", arguments={"order_by": ["overrides:asc"]}, ctx=USER)
    assert result.is_error and _typ(result) == "validation-error", result.output
    assert _OVERRIDE_PASSWORD not in result.output and "ws-one" not in result.output


@pytest.mark.asyncio
async def test_a_plain_field_still_sorts_and_the_result_is_masked(toolset) -> None:
    result = await toolset.call(tool_name="list_workspace_templates", arguments={"order_by": ["provider_id:asc"]}, ctx=USER)
    assert not result.is_error, result.output
    assert "tpl-a" in result.output and _PASSWORD not in result.output and "**********" in result.output
