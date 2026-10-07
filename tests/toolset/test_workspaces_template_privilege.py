"""The workspaces toolset applies the REST rule on admin-only template fields (security review 2026-10-08, INJ-02).

``create_workspace_template`` and ``update_workspace_template`` are user-tier tools, so a role=user caller (over MCP, or
an agent run a user started) could write a template with host mounts, a Kubernetes overlay or a secret file source that
REST now refuses. The tools take the caller from the tool context (an agent run's ``initiated_by``) or, over MCP, from
the request's actor; with neither, they fail closed. ``create_workspace`` refuses a secret file source in the overrides
the same way.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primer.api.registries import WorkspaceRegistry
from primer.mcp.server import current_actor
from primer.model.principal import Principal, PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.workspace import Workspace, WorkspaceTemplate
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.workspaces import build_workspaces_toolset
from tests.toolset.test_workspaces import _StubBackend


@pytest.fixture
async def world(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    registry = WorkspaceRegistry(sp, factory=_StubBackend)
    toolset = build_workspaces_toolset(storage_provider=sp, workspace_registry=registry)
    yield sp, toolset
    await sp.aclose()


def _ctx(role: str | None, kind: str = "user") -> ToolContext:
    return ToolContext(
        tool_call_id="call-1", session_id="sess-1", workspace_id=None,
        initiated_by=PrincipalRef(type=kind, id=f"{kind}-1", display=kind, role=role, source="local"),
    )


async def _call(toolset, name: str, *, ctx: ToolContext | None = None, **args):
    result = await toolset.call(tool_name=name, arguments=args, ctx=ctx)
    try:
        body = json.loads(result.output)
    except ValueError:
        body = result.output
    return result.is_error, body


_PRIVILEGED = {
    "extra_mounts": {
        "id": "tpl-c", "provider_id": "p-1", "description": "d",
        "backend": {"kind": "container", "image": "alpine:3", "extra_mounts": [{"host": "/", "container": "/host"}]},
    },
    "pod_overrides": {
        "id": "tpl-k", "provider_id": "p-1", "description": "d",
        "backend": {"kind": "kubernetes", "image": "alpine:3", "pod_overrides": {"dnsPolicy": "ClusterFirst"}},
    },
    "files.secret": {
        "id": "tpl-s", "provider_id": "p-1", "description": "d",
        "files": [{"path": "creds", "source": {"kind": "secret", "name": "OPENAI_API_KEY"}}],
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", sorted(_PRIVILEGED))
async def test_a_user_run_cannot_create_a_template_with_an_admin_only_field(world, field) -> None:
    sp, toolset = world
    body = _PRIVILEGED[field]

    is_error, answer = await _call(toolset, "create_workspace_template", ctx=_ctx("user"), entity=body)

    assert is_error and answer["type"] == "forbidden", answer
    assert field in answer["message"]
    assert await sp.get_storage(WorkspaceTemplate).get(body["id"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", sorted(_PRIVILEGED))
async def test_an_admin_run_can_create_a_template_with_an_admin_only_field(world, field) -> None:
    sp, toolset = world
    body = _PRIVILEGED[field]

    is_error, answer = await _call(toolset, "create_workspace_template", ctx=_ctx("admin"), entity=body)

    assert not is_error, answer
    assert await sp.get_storage(WorkspaceTemplate).get(body["id"]) is not None


@pytest.mark.asyncio
async def test_a_user_run_cannot_add_an_admin_only_field_on_update(world) -> None:
    sp, toolset = world
    plain = {"id": "tpl-k", "provider_id": "p-1", "description": "d", "backend": {"kind": "kubernetes", "image": "alpine:3"}}
    assert not (await _call(toolset, "create_workspace_template", ctx=_ctx("user"), entity=plain))[0]

    is_error, answer = await _call(
        toolset, "update_workspace_template", ctx=_ctx("user"), id="tpl-k", entity=_PRIVILEGED["pod_overrides"],
    )

    assert is_error and answer["type"] == "forbidden", answer
    stored = await sp.get_storage(WorkspaceTemplate).get("tpl-k")
    assert stored.backend.pod_overrides is None


@pytest.mark.asyncio
async def test_an_mcp_user_cannot_create_a_template_with_an_admin_only_field(world) -> None:
    sp, toolset = world
    token = current_actor.set(Principal(type="user", id="u", display="u", role="user", source="local"))
    try:
        is_error, answer = await _call(toolset, "create_workspace_template", entity=_PRIVILEGED["extra_mounts"])
    finally:
        current_actor.reset(token)

    assert is_error and answer["type"] == "forbidden", answer
    assert await sp.get_storage(WorkspaceTemplate).get("tpl-c") is None


@pytest.mark.asyncio
async def test_an_mcp_admin_can_create_a_template_with_an_admin_only_field(world) -> None:
    sp, toolset = world
    token = current_actor.set(Principal(type="user", id="a", display="a", role="admin", source="local"))
    try:
        is_error, answer = await _call(toolset, "create_workspace_template", entity=_PRIVILEGED["extra_mounts"])
    finally:
        current_actor.reset(token)

    assert not is_error, answer


@pytest.mark.asyncio
async def test_a_call_with_no_known_caller_fails_closed(world) -> None:
    sp, toolset = world

    is_error, answer = await _call(toolset, "create_workspace_template", entity=_PRIVILEGED["files.secret"])

    assert is_error and answer["type"] == "forbidden", answer
    assert await sp.get_storage(WorkspaceTemplate).get("tpl-s") is None


@pytest.mark.asyncio
async def test_a_call_with_no_known_caller_may_still_write_a_plain_template(world) -> None:
    _, toolset = world
    plain = {"id": "tpl-p", "provider_id": "p-1", "description": "d", "init_commands": ["echo hi"]}

    is_error, answer = await _call(toolset, "create_workspace_template", entity=plain)

    assert not is_error, answer


@pytest.mark.asyncio
async def test_a_user_run_cannot_mount_a_secret_through_workspace_overrides(world) -> None:
    sp, toolset = world
    await sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(id="tpl-p", provider_id="p-1", description="d"))

    is_error, answer = await _call(
        toolset, "create_workspace", ctx=_ctx("user"), template_id="tpl-p",
        overrides={"files": [{"path": "creds", "source": {"kind": "secret", "name": "OPENAI_API_KEY"}}]},
    )

    assert is_error and answer["type"] == "forbidden", answer
    assert (await sp.get_storage(Workspace).list(OffsetPage(offset=0, length=10))).items == []
