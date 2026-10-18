"""``workspaces::list_workspace_provider_choices``: the tool twin of ``GET /v1/workspace_providers/_choices`` (platform revamp 2026-10, plan 08 task 7).

``list_workspace_providers`` is admin-only because a provider row carries its connection. A user-run agent still has to put a provider id in
``create_workspace_template``, so this user-tier tool lists every provider by id, kind and label (and the image a container provider runs when a
template names none), and nothing of the connection. Pinned here on a real SQLite store through the real toolset.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primer.api.registries import WorkspaceRegistry
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.workspace import WorkspaceProvider
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.workspaces import build_workspaces_toolset
from primer.workspace import provider_choices
from tests.toolset._desc_conformance import assert_tool_conforms
from tests.toolset.test_workspaces import _StubBackend

_TOKEN = "sa-token-that-must-never-be-served"


@pytest.fixture
async def world(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    toolset = build_workspaces_toolset(storage_provider=sp, workspace_registry=WorkspaceRegistry(sp, factory=_StubBackend))
    yield sp, toolset
    await sp.aclose()


def _ctx(role: str) -> ToolContext:
    return ToolContext(
        tool_call_id="call-1", session_id="sess-1", workspace_id=None,
        initiated_by=PrincipalRef(type="user", id="user-1", display="user", role=role, source="local"),
    )


async def _call(toolset, name: str, *, ctx: ToolContext | None = None, **args):
    result = await toolset.call(tool_name=name, arguments=args, ctx=ctx)
    try:
        body = json.loads(result.output)
    except ValueError:
        body = result.output
    return result.is_error, body


def _container(provider_id: str, runtime: str = "docker") -> WorkspaceProvider:
    return WorkspaceProvider.model_validate({
        "id": provider_id, "provider": "container",
        "config": {
            "kind": "container", "runtime": runtime,
            "connection": {"kind": "socket", "socket_path": "/var/run/docker.sock"},
            "reachability": {"kind": "host_port", "bind_host": "127.0.0.1"},
        },
    })


def _kubernetes(provider_id: str) -> WorkspaceProvider:
    return WorkspaceProvider.model_validate({
        "id": provider_id, "provider": "kubernetes",
        "config": {
            "kind": "kubernetes", "variant": "system", "namespace": "primer-private-namespace",
            "connection": {"kind": "service_account_token", "apiserver_url": "https://10.9.9.9:6443", "ca_data": "PEM", "token": _TOKEN},
            "reachability": {"kind": "in_cluster"},
        },
    })


async def _tool(toolset, name: str):
    async for tool in toolset.list_tools():
        if tool.id == name:
            return tool
    raise AssertionError(f"the toolset has no tool {name!r}")


@pytest.mark.asyncio
async def test_the_tool_is_user_tier_and_conforms(world) -> None:
    _, toolset = world

    tool = await _tool(toolset, "list_workspace_provider_choices")

    assert tool.required_role == "user"
    assert_tool_conforms(tool)
    assert tool.args_schema.get("required", []) == [], "the tool takes no arguments"


@pytest.mark.asyncio
async def test_a_user_run_gets_the_same_choices_as_the_route(world) -> None:
    sp, toolset = world
    for row in (_kubernetes("k3s"), _container("dock")):
        await sp.get_storage(WorkspaceProvider).create(row)

    is_error, answer = await _call(toolset, "list_workspace_provider_choices", ctx=_ctx("user"))

    assert not is_error, answer
    assert answer["items"] == [
        {"id": "dock", "kind": "container", "label": "Docker", "default_image": "primer/workspace-runtime:1.1"},
        {"id": "k3s", "kind": "kubernetes", "label": "Kubernetes", "default_image": None},
    ]


@pytest.mark.asyncio
async def test_the_choices_serve_nothing_of_the_connection(world) -> None:
    sp, toolset = world
    await sp.get_storage(WorkspaceProvider).create(_kubernetes("k3s"))

    result = await toolset.call(tool_name="list_workspace_provider_choices", arguments={}, ctx=_ctx("user"))

    for secret in (_TOKEN, "10.9.9.9", "PEM", "primer-private-namespace", "config"):
        assert secret not in result.output, secret


@pytest.mark.asyncio
async def test_the_full_provider_listing_tool_stays_admin_only(world) -> None:
    _, toolset = world

    tool = await _tool(toolset, "list_workspace_providers")

    assert tool.required_role == "admin"


@pytest.mark.asyncio
async def test_the_tool_pages_through_every_provider(world, monkeypatch) -> None:
    sp, toolset = world
    monkeypatch.setattr(provider_choices, "_PAGE", 2)
    for n in (3, 1, 2, 5, 4):
        await sp.get_storage(WorkspaceProvider).create(_container(f"p-{n}"))

    is_error, answer = await _call(toolset, "list_workspace_provider_choices", ctx=_ctx("user"))

    assert not is_error, answer
    assert [item["id"] for item in answer["items"]] == [f"p-{n}" for n in (1, 2, 3, 4, 5)]


@pytest.mark.asyncio
async def test_no_providers_is_an_empty_list_not_an_error(world) -> None:
    _, toolset = world

    is_error, answer = await _call(toolset, "list_workspace_provider_choices", ctx=_ctx("user"))

    assert not is_error and answer == {"items": []}
