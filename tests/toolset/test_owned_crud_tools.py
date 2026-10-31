"""Ownership rules on the system, crud and workspaces toolsets' CRUD tools (ACL-P1 T8b).

The same rules the REST ``make_crud_router(owned=True)`` applies (Task 5) apply to the
tools: the caller is the run's ``initiated_by`` (or, with no context, the MCP request's
actor); a list is confined to the caller's visibility predicate; a row the caller may not
read answers exactly like a missing id; create stamps the caller as owner and ignores any
``access`` in the body; update needs ``full`` and keeps the stored access; delete needs
``owner``. Admins see and reach every row.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.channel import Channel
from primer.model.graph import Graph
from primer.model.principal import PrincipalRef
from primer.model.providers.toolset import PythonConfig, Toolset, ToolsetProviderType
from primer.model.workspace import WorkspaceTemplate
from primer.model.yield_ import ToolContext
from primer.toolset.crud import build_crud_toolset
from primer.toolset.system import build_system_toolset
from primer.toolset.workspaces import build_workspaces_toolset
from tests._support.workspace_providers import seed_public_workspace_providers

A, B, C = "u-a", "u-b", "u-c"
ADMIN = "u-admin"

PYTHON_SOURCE_A = (
    "@primer_tool()\n"
    "def greet(name: str) -> str:\n"
    '    """Greet a person.\n\n'
    "    Use when greeting someone.\n\n"
    '    Args:\n        name: Who to greet.\n    """\n'
    "    return 'hi ' + name\n"
)
PYTHON_SOURCE_B = (
    "@primer_tool()\n"
    "def greet(name: str) -> str:\n"
    '    """Greet a person.\n\n'
    "    Use when greeting someone.\n\n"
    '    Args:\n        name: Who to greet.\n    """\n'
    "    return 'yo ' + name\n"
)


# ===========================================================================
# Identities and call plumbing
# ===========================================================================


def _user(uid: str, role: str = "user") -> PrincipalRef:
    return PrincipalRef(type="user", id=uid, display=uid, role=role, source="local", user_id=uid)


def _call(
    provider: Any, tool: str, arguments: dict[str, Any], ref: PrincipalRef | None,
) -> Any:
    """One tool call attributed to ``ref`` (``None`` for a call with no identity)."""
    return provider.call(
        tool_name=tool, arguments=arguments,
        ctx=ToolContext(tool_call_id="call-1", session_id="sess-1", workspace_id=None, initiated_by=ref),
    )


def _type(result: Any) -> str:
    assert result.is_error, result.output
    return json.loads(result.output)["type"]


def _message(result: Any) -> str:
    assert result.is_error, result.output
    return json.loads(result.output)["message"]


def _ids(result: Any) -> set[str]:
    assert not result.is_error, result.output
    return {item["id"] for item in json.loads(result.output)["items"]}


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
async def sp(fake_storage_provider):
    await seed_public_workspace_providers(fake_storage_provider, "local-1")
    return fake_storage_provider


@pytest.fixture
def pr(sp):
    return ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
    )


@pytest.fixture
def system(sp, pr):
    return build_system_toolset(storage_provider=sp, provider_registry=pr)  # type: ignore[arg-type]


@pytest.fixture
def crud(sp):
    return build_crud_toolset(storage_provider=sp)  # type: ignore[arg-type]


@pytest.fixture
def ws(sp):
    return build_workspaces_toolset(
        storage_provider=sp, workspace_registry=WorkspaceRegistry(sp),  # type: ignore[arg-type]
    )


# ===========================================================================
# Row factories (seeded straight into storage, as a stored row would be)
# ===========================================================================


def _agent_row(aid: str, owner: str, **access: Any) -> Agent:
    return Agent(
        id=aid, description=aid, model=AgentModel(profile_id="p--m"),
        tools=[], system_prompt=[], access=Access(owners=[owner], **access),
    )


def _toolset_row(tid: str, owner: str, **access: Any) -> Toolset:
    return Toolset(
        id=tid, provider=ToolsetProviderType.PYTHON,
        config=PythonConfig(source=PYTHON_SOURCE_A, source_version=1, default_timeout_seconds=30.0),
        access=Access(owners=[owner], **access),
    )


def _template_row(tid: str, owner: str, **access: Any) -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id=tid, description=tid, provider_id="local-1", access=Access(owners=[owner], **access),
    )


def _agent_body(aid: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": aid, "description": aid, "model": {"profile_id": "p--m"},
        "tools": [], "system_prompt": [], **extra,
    }


def _template_body(tid: str, **extra: Any) -> dict[str, Any]:
    return {"id": tid, "description": tid, "provider_id": "local-1", **extra}


# ===========================================================================
# The system toolset: the full agent matrix
# ===========================================================================


@pytest.mark.asyncio
async def test_list_agents_omits_others_private_rows(sp, system) -> None:
    await sp.get_storage(Agent).create(_agent_row("ag-a", A))
    await sp.get_storage(Agent).create(_agent_row("ag-b", B))
    await sp.get_storage(Agent).create(_agent_row("ag-public", B, public=True))
    assert _ids(await _call(system, "list_agents", {}, _user(B))) == {"ag-b", "ag-public"}


@pytest.mark.asyncio
async def test_a_call_without_a_context_acts_as_the_mcp_request_actor(sp, system) -> None:
    from primer.mcp.server import current_actor
    from primer.model.principal import Principal

    await sp.get_storage(Agent).create(_agent_row("ag-a", A))
    await sp.get_storage(Agent).create(_agent_row("ag-b", B))
    actor = Principal(type="user", id="mcp-b", display=B, role="user", source="local", user_id=B)
    token = current_actor.set(actor)
    try:
        res = await system.call(tool_name="list_agents", arguments={}, ctx=None)
    finally:
        current_actor.reset(token)
    assert _ids(res) == {"ag-b"}


@pytest.mark.asyncio
async def test_get_agent_answers_a_private_row_exactly_like_a_missing_id(sp, system) -> None:
    await sp.get_storage(Agent).create(_agent_row("ag-a", A))
    missing = await _call(system, "get_agent", {"id": "no-such-agent"}, _user(B))
    private = await _call(system, "get_agent", {"id": "ag-a"}, _user(B))
    assert _type(missing) == _type(private) == "not-found"
    assert _message(missing) == "Agent 'no-such-agent' does not exist"
    assert _message(private) == "Agent 'ag-a' does not exist"


@pytest.mark.asyncio
async def test_get_agent_serves_a_row_shared_at_read_level(sp, system) -> None:
    await sp.get_storage(Agent).create(_agent_row("ag-a", A, grants={B: "read"}))
    res = await _call(system, "get_agent", {"id": "ag-a"}, _user(B))
    assert not res.is_error
    assert json.loads(res.output)["access"]["owners"] == [A]


@pytest.mark.asyncio
async def test_update_agent_needs_full_access(sp, system) -> None:
    await sp.get_storage(Agent).create(_agent_row("ag-a", A, grants={B: "read"}))
    body = (await sp.get_storage(Agent).get("ag-a")).model_dump(mode="json")
    res = await _call(system, "update_agent", {"id": "ag-a", "entity": body}, _user(B))
    assert _type(res) == "forbidden"
    stranger = await _call(system, "update_agent", {"id": "ag-a", "entity": body}, _user(C))
    assert _type(stranger) == "not-found"
    assert (await sp.get_storage(Agent).get("ag-a")).access.owners == [A]


@pytest.mark.asyncio
async def test_create_agent_stamps_the_caller_as_owner_and_ignores_the_body_access(sp, system) -> None:
    body = _agent_body("ag-new", access={"public": True, "owners": [A]})
    res = await _call(system, "create_agent", {"entity": body}, _user(B))
    assert not res.is_error
    row = await sp.get_storage(Agent).get("ag-new")
    assert row.access.owners == [B]
    assert row.access.public is False
    assert row.access.members == [f"u:{B}"]


@pytest.mark.asyncio
async def test_create_agent_without_a_user_identity_is_admin_managed(sp, system) -> None:
    res = await _call(system, "create_agent", {"entity": _agent_body("ag-sys")}, PrincipalRef.system())
    assert not res.is_error
    row = await sp.get_storage(Agent).get("ag-sys")
    assert row.access.owners == [] and row.access.members == []


@pytest.mark.asyncio
async def test_update_agent_keeps_the_stored_access(sp, system) -> None:
    await sp.get_storage(Agent).create(_agent_row("ag-b", B))
    body = (await sp.get_storage(Agent).get("ag-b")).model_dump(mode="json")
    body["description"] = "edited"
    body["access"] = {"public": True, "grants": {A: "full"}}
    res = await _call(system, "update_agent", {"id": "ag-b", "entity": body}, _user(B))
    assert not res.is_error
    row = await sp.get_storage(Agent).get("ag-b")
    assert row.description == "edited"
    assert row.access.owners == [B] and row.access.public is False and row.access.grants == {}


@pytest.mark.asyncio
async def test_delete_agent_needs_owner(sp, system) -> None:
    await sp.get_storage(Agent).create(_agent_row("ag-a", A, grants={B: "full"}))
    res = await _call(system, "delete_agent", {"id": "ag-a"}, _user(B))
    assert _type(res) == "forbidden"
    assert await sp.get_storage(Agent).get("ag-a") is not None
    stranger = await _call(system, "delete_agent", {"id": "ag-a"}, _user(C))
    assert _type(stranger) == "not-found"
    ok = await _call(system, "delete_agent", {"id": "ag-a"}, _user(A))
    assert not ok.is_error
    assert await sp.get_storage(Agent).get("ag-a") is None


@pytest.mark.asyncio
async def test_admin_sees_and_reaches_every_agent(sp, system) -> None:
    await sp.get_storage(Agent).create(_agent_row("ag-a", A))
    admin = _user(ADMIN, role="admin")
    assert "ag-a" in _ids(await _call(system, "list_agents", {}, admin))
    assert not (await _call(system, "get_agent", {"id": "ag-a"}, admin)).is_error


@pytest.mark.asyncio
async def test_find_agents_omits_others_private_rows(sp, system) -> None:
    await sp.get_storage(Agent).create(_agent_row("ag-a", A))
    await sp.get_storage(Agent).create(_agent_row("ag-b", B))
    predicate = {
        "kind": "predicate", "left": {"kind": "field", "name": "id"},
        "op": "~=", "right": {"kind": "value", "value": "ag-%"},
    }
    assert _ids(await _call(system, "find_agents", {"predicate": predicate}, _user(B))) == {"ag-b"}
    admin = _user(ADMIN, role="admin")
    assert _ids(await _call(system, "find_agents", {"predicate": predicate}, admin)) == {"ag-a", "ag-b"}


@pytest.mark.asyncio
async def test_the_other_owned_entities_hide_private_rows_the_same_way(sp, system) -> None:
    await sp.get_storage(Graph).create(Graph(id="gr-a", description="gr-a", access=Access(owners=[A])))
    await sp.get_storage(Toolset).create(_toolset_row("ts-a", A))
    await sp.get_storage(Channel).create(
        Channel(id="ch-a", provider_id="cp-1", provider="slack", external_id="C1", access=Access(owners=[A])),
    )
    assert "gr-a" not in _ids(await _call(system, "list_graphs", {}, _user(B)))
    assert "ts-a" not in _ids(await _call(system, "list_toolsets", {}, _user(B)))
    assert "ch-a" not in _ids(await _call(system, "list_channels", {}, _user(B)))
    for tool, rid in (("get_graph", "gr-a"), ("get_toolset", "ts-a"), ("get_channel", "ch-a")):
        res = await _call(system, tool, {"id": rid}, _user(B))
        assert _type(res) == "not-found", (tool, res.output)


# ===========================================================================
# The crud toolset: the python-toolset trio
# ===========================================================================


@pytest.mark.asyncio
async def test_crud_create_agent_stamps_the_caller_as_owner(sp, crud) -> None:
    res = await _call(crud, "create_agent", {"entity": {"id": "ag-c", "description": "ag-c", "model": {"profile_id": "p--m"}}}, _user(B))
    assert not res.is_error, res.output
    row = await sp.get_storage(Agent).get("ag-c")
    assert row.access.owners == [B]


@pytest.mark.asyncio
async def test_crud_update_agent_needs_full_and_a_stranger_reads_nothing(sp, crud) -> None:
    await sp.get_storage(Agent).create(_agent_row("ag-a", A, grants={B: "read"}))
    body = _agent_body("ag-a")
    reader = await _call(crud, "update_agent", {"id": "ag-a", "entity": body}, _user(B))
    assert _type(reader) == "forbidden"
    stranger = await _call(crud, "update_agent", {"id": "ag-a", "entity": body}, _user(C))
    assert _type(stranger) == "not-found"
    assert (await sp.get_storage(Agent).get("ag-a")).description == "ag-a"


@pytest.mark.asyncio
async def test_crud_create_python_toolset_stamps_the_caller(sp, crud) -> None:
    res = await _call(crud, "create_python_toolset", {"toolset_id": "py-b", "source": PYTHON_SOURCE_A}, _user(B))
    assert not res.is_error
    assert json.loads(res.output)["ok"] is True
    row = await sp.get_storage(Toolset).get("py-b")
    assert row.access.owners == [B] and row.access.members == [f"u:{B}"]


@pytest.mark.asyncio
async def test_crud_update_python_toolset_source_needs_full_access(sp, crud) -> None:
    await sp.get_storage(Toolset).create(_toolset_row("py-a", A, grants={B: "read"}))
    res = await _call(crud, "update_python_toolset_source", {"toolset_id": "py-a", "source": PYTHON_SOURCE_B}, _user(B))
    assert _type(res) == "forbidden"
    stranger = await _call(crud, "update_python_toolset_source", {"toolset_id": "py-a", "source": PYTHON_SOURCE_B}, _user(C))
    assert json.loads(stranger.output) == {"ok": False, "error": "not-found"}
    row = await sp.get_storage(Toolset).get("py-a")
    assert row.config.source == PYTHON_SOURCE_A


@pytest.mark.asyncio
async def test_crud_list_python_tools_omits_others_private_toolsets(sp, crud) -> None:
    await sp.get_storage(Toolset).create(_toolset_row("py-a", A))
    await sp.get_storage(Toolset).create(_toolset_row("py-b", B))
    res = await _call(crud, "list_python_tools", {"toolset_id": "py-a"}, _user(B))
    assert json.loads(res.output) == {"ok": False, "error": "not-found"}
    own = await _call(crud, "list_python_tools", {"toolset_id": "py-b"}, _user(B))
    assert json.loads(own.output)["ok"] is True


# ===========================================================================
# The workspaces toolset: the workspace-template matrix
# ===========================================================================


@pytest.mark.asyncio
async def test_list_workspace_templates_omits_others_private_rows(sp, ws) -> None:
    await sp.get_storage(WorkspaceTemplate).create(_template_row("tpl-a", A))
    await sp.get_storage(WorkspaceTemplate).create(_template_row("tpl-b", B))
    await sp.get_storage(WorkspaceTemplate).create(_template_row("tpl-public", B, public=True))
    assert _ids(await _call(ws, "list_workspace_templates", {}, _user(B))) == {"tpl-b", "tpl-public"}


@pytest.mark.asyncio
async def test_get_workspace_template_answers_a_private_row_like_a_missing_id(sp, ws) -> None:
    await sp.get_storage(WorkspaceTemplate).create(_template_row("tpl-a", A))
    missing = await _call(ws, "get_workspace_template", {"id": "no-such-template"}, _user(B))
    private = await _call(ws, "get_workspace_template", {"id": "tpl-a"}, _user(B))
    assert _type(missing) == _type(private) == "not-found"
    assert _message(missing) == "WorkspaceTemplate 'no-such-template' does not exist"
    assert _message(private) == "WorkspaceTemplate 'tpl-a' does not exist"


@pytest.mark.asyncio
async def test_create_workspace_template_stamps_the_caller_and_ignores_the_body_access(sp, ws) -> None:
    res = await _call(
        ws, "create_workspace_template",
        {"entity": _template_body("tpl-new", access={"public": True, "owners": [A]})}, _user(B),
    )
    assert not res.is_error
    row = await sp.get_storage(WorkspaceTemplate).get("tpl-new")
    assert row.access.owners == [B] and row.access.public is False and row.access.members == [f"u:{B}"]


@pytest.mark.asyncio
async def test_update_workspace_template_needs_full_and_keeps_the_stored_access(sp, ws) -> None:
    await sp.get_storage(WorkspaceTemplate).create(_template_row("tpl-a", A, grants={B: "read"}))
    body = (await sp.get_storage(WorkspaceTemplate).get("tpl-a")).model_dump(mode="json")
    body["description"] = "edited"
    body["access"] = {"public": True}
    res = await _call(ws, "update_workspace_template", {"id": "tpl-a", "entity": body}, _user(B))
    assert _type(res) == "forbidden"
    stored = await sp.get_storage(WorkspaceTemplate).get("tpl-a")
    body = stored.model_dump(mode="json")
    body["description"] = "edited"
    body["access"] = {"public": True}
    ok = await _call(ws, "update_workspace_template", {"id": "tpl-a", "entity": body}, _user(A))
    assert not ok.is_error
    final = await sp.get_storage(WorkspaceTemplate).get("tpl-a")
    assert final.description == "edited"
    assert final.access.owners == [A] and final.access.public is False


@pytest.mark.asyncio
async def test_delete_workspace_template_needs_owner(sp, ws) -> None:
    await sp.get_storage(WorkspaceTemplate).create(_template_row("tpl-a", A, grants={B: "full"}))
    res = await _call(ws, "delete_workspace_template", {"id": "tpl-a"}, _user(B))
    assert _type(res) == "forbidden"
    stranger = await _call(ws, "delete_workspace_template", {"id": "tpl-a"}, _user(C))
    assert _type(stranger) == "not-found"
    ok = await _call(ws, "delete_workspace_template", {"id": "tpl-a"}, _user(A))
    assert not ok.is_error
    assert await sp.get_storage(WorkspaceTemplate).get("tpl-a") is None


@pytest.mark.asyncio
async def test_admin_sees_and_reaches_every_template(sp, ws) -> None:
    await sp.get_storage(WorkspaceTemplate).create(_template_row("tpl-a", A))
    admin = _user(ADMIN, role="admin")
    assert "tpl-a" in _ids(await _call(ws, "list_workspace_templates", {}, admin))
    assert not (await _call(ws, "get_workspace_template", {"id": "tpl-a"}, admin)).is_error
