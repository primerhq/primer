"""The template tools serve a url file source without its password and keep it through an update (ticket 01a11d32).

``get_workspace_template`` and ``list_workspace_templates`` are user-tier tools: their results go into a transcript and to the model vendor. ``update_workspace_template`` is a full replace, so an agent that
reads a template and writes it back (changing one field) used to store the mask as the password. The tools run the REST rules: the served form is masked, an update of the served body keeps the
stored password for the same scheme, host, port and user, and another host or user answers ``type=validation-error`` and stores nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primer.api.registries import WorkspaceRegistry
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.workspace import WorkspaceTemplate
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.workspaces import build_workspaces_toolset
from tests.toolset.test_workspaces import _StubBackend

MASK = "**********"
URL = "https://reader:s3cr3t@files.example.com/seed.txt"
MASKED = f"https://reader:{MASK}@files.example.com/seed.txt"
CTX = ToolContext(
    tool_call_id="call-1", session_id="sess-1", workspace_id=None,
    initiated_by=PrincipalRef(type="user", id="user-1", display="user", role="admin", source="local"),
)


@pytest.fixture
async def world(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    toolset = build_workspaces_toolset(storage_provider=sp, workspace_registry=WorkspaceRegistry(sp, factory=_StubBackend))
    yield sp, toolset
    await sp.aclose()


def _template(url: str = URL, row_id: str = "tpl-a", description: str = "d") -> dict:
    return {"id": row_id, "provider_id": "p-1", "description": description, "files": [{"path": "seed.txt", "source": {"kind": "url", "url": url}}]}


async def _call(toolset, name: str, **args):
    result = await toolset.call(tool_name=name, arguments=args, ctx=CTX)
    try:
        body = json.loads(result.output)
    except ValueError:
        body = result.output
    return result.is_error, body, result.output


def _url(body: dict) -> str:
    return body["files"][0]["source"]["url"]


async def _stored_url(sp, row_id: str = "tpl-a") -> str:
    return str((await sp.get_storage(WorkspaceTemplate).get(row_id)).files[0].source.url)


@pytest.mark.asyncio
async def test_create_get_and_list_results_carry_no_password(world) -> None:
    sp, toolset = world

    failed, created, raw_created = await _call(toolset, "create_workspace_template", entity=_template())
    _, got, raw_got = await _call(toolset, "get_workspace_template", id="tpl-a")
    _, listed, raw_listed = await _call(toolset, "list_workspace_templates")

    assert not failed, created
    for raw in (raw_created, raw_got, raw_listed):
        assert "s3cr3t" not in raw, raw
    assert _url(created) == MASKED and _url(got) == MASKED
    assert [_url(item) for item in listed["items"] if item["id"] == "tpl-a"] == [MASKED]
    assert await _stored_url(sp) == URL


@pytest.mark.asyncio
async def test_an_update_of_the_served_body_keeps_the_stored_password(world) -> None:
    sp, toolset = world
    await _call(toolset, "create_workspace_template", entity=_template())
    _, served, _ = await _call(toolset, "get_workspace_template", id="tpl-a")
    served["description"] = "edited"

    failed, updated, raw = await _call(toolset, "update_workspace_template", id="tpl-a", entity=served)

    assert not failed, updated
    assert "s3cr3t" not in raw
    assert await _stored_url(sp) == URL
    assert (await sp.get_storage(WorkspaceTemplate).get("tpl-a")).description == "edited"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "moved",
    [
        pytest.param(f"https://reader:{MASK}@attacker.example/seed.txt", id="another host"),
        pytest.param(f"https://other:{MASK}@files.example.com/seed.txt", id="another username"),
    ],
)
async def test_an_update_whose_mask_cannot_be_restored_is_a_validation_error_and_stores_nothing(world, moved: str) -> None:
    sp, toolset = world
    await _call(toolset, "create_workspace_template", entity=_template())
    _, served, _ = await _call(toolset, "get_workspace_template", id="tpl-a")
    served["files"][0]["source"]["url"] = moved

    failed, body, raw = await _call(toolset, "update_workspace_template", id="tpl-a", entity=served)

    assert failed and body["type"] == "validation-error", body
    assert "re-enter the password" in raw and "s3cr3t" not in raw
    assert await _stored_url(sp) == URL


@pytest.mark.asyncio
async def test_an_update_with_a_new_password_stores_it(world) -> None:
    sp, toolset = world
    await _call(toolset, "create_workspace_template", entity=_template())
    _, served, _ = await _call(toolset, "get_workspace_template", id="tpl-a")
    served["files"][0]["source"]["url"] = "https://reader:newpass@files.example.com/seed.txt"

    failed, body, raw = await _call(toolset, "update_workspace_template", id="tpl-a", entity=served)

    assert not failed and "newpass" not in raw, body
    assert await _stored_url(sp) == "https://reader:newpass@files.example.com/seed.txt"


