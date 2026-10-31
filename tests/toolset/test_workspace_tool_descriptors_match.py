"""Tool descriptions that said something their handlers do not do (lead ruling 4 of tickets 01a16d72 / 01a16d3c-b609).

Found by the platform-wiki writers (#896, #902): an agent reads the description and nothing else, so each sentence below is pinned against the
code that decides it, not against a copy of the words.

* the template tools' admin-only list left out ``container_security_context_overrides``, which ``template_privilege`` gates;
* ``system__reset_prime_instructions`` named ``forbidden``, ``not-found`` and ``unavailable``, and its handler also answers ``conflict`` (a
  workspace the deployment refuses) and ``bad-request`` (a directory called ``agents.md``);
* ``message_session`` said not-found is "no such session in your workspace" and ids come from ``list_my_sessions``: a session in a
  workspace the caller's person holds no access to answers as an unknown id (access-control P2 T6), and its ids come from
  ``find_sessions``;
* ``restart_workspace_session`` said it "works for completed, failed, or cancelled sessions": the tools refuse a cancelled one, whoever calls;
* ``get_workspace_session`` answers no ``session_state`` and no ``parked_event_key`` and did not say where to read them.
"""

from __future__ import annotations

import json

import pytest

from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.model.workspace import Workspace
from primer.model.workspace_refusal import WorkspaceRefusedError
from primer.toolset.system import build_system_toolset
from primer.toolset.workspaces import build_workspaces_toolset
from primer.workspace.template_privilege import _gated_values
from tests.bootstrap.test_prime_agents_md import _Registry, _row, _Ws
from tests.toolset.test_workspaces import _SP, _StubBackend

_TEMPLATE_TOOLS = ["create_workspace_template", "update_workspace_template"]


@pytest.fixture
async def tools():
    sp = _SP()
    toolset = build_workspaces_toolset(storage_provider=sp, workspace_registry=WorkspaceRegistry(sp, factory=_StubBackend))
    return {tool.id: tool async for tool in toolset.list_tools()}


def _admin_only_sentence(description: str) -> str:
    return description.split("The admin-only fields", 1)[1].split("are open to every user", 1)[0]


# ---- the template tools ----------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", _TEMPLATE_TOOLS)
def test_the_admin_only_list_names_every_field_template_privilege_gates(tools, name) -> None:
    """Derived from the gate itself, so a field added to it fails here until the description names it."""
    sentence = _admin_only_sentence(tools[name].description)
    gated = list(_gated_values(None))
    assert "backend.container_security_context_overrides" in gated

    for field in gated:
        leaf = {"files.secret": "secret"}.get(field, field.removeprefix("backend."))
        assert leaf in sentence, f"{name}: the admin-only list leaves out {field}"


# ---- system__reset_prime_instructions ---------------------------------------------------------------------------------------------------------


def _provider_registry(sp) -> ProviderRegistry:
    return ProviderRegistry(
        sp, llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda t: object(),
    )


@pytest.fixture
async def system(fake_storage_provider):
    from primer.model.access import Access

    row = _row("prime")
    row.access = Access(owners=["u1"])
    await fake_storage_provider.get_storage(Workspace).create(row)
    await fake_storage_provider.set_default_workspace_id("prime")
    registry = _Registry(_Ws({"agents.md": b"mine"}))
    toolset = build_system_toolset(
        storage_provider=fake_storage_provider, provider_registry=_provider_registry(fake_storage_provider), workspace_registry=registry,
    )
    return toolset, registry


async def test_the_reset_tool_names_every_type_it_answers(system) -> None:
    toolset, _ = system
    description = {tool.id: tool async for tool in toolset.list_tools()}["reset_prime_instructions"].description

    for answer in ("type=forbidden", "type=not-found", "type=unavailable", "type=conflict", "type=bad-request"):
        assert answer in description, answer
    assert "refuses" in description.split("type=conflict", 1)[0].rsplit(";", 1)[-1], "the conflict is a workspace the deployment refuses"
    assert "directory" in description.split("type=bad-request", 1)[0].rsplit(";", 1)[-1], "the bad request is a directory named agents.md"


async def test_a_workspace_the_deployment_refuses_is_a_conflict(system) -> None:
    """The answer the description now names, from the handler (the bad-request one is pinned in test_system_reset_prime_instructions.py)."""
    from primer.model.principal import Principal
    from primer.mcp.server import current_actor

    toolset, registry = system

    async def _refused(workspace_id):
        raise WorkspaceRefusedError("provider 'local' refused", provider_id="local").for_workspace(workspace_id)

    registry.get_workspace = _refused
    # The access check asks the MCP endpoint's token user, which the real dispatcher publishes (``primer.api._app_mcp``).
    token = current_actor.set(Principal(type="user", id="tok-u1", display="u1", role="user", source="local", user_id="u1"))
    try:
        result = await toolset.call(tool_name="reset_prime_instructions", arguments={})
    finally:
        current_actor.reset(token)

    assert result.is_error and json.loads(result.output)["type"] == "conflict"


# ---- the session tools -----------------------------------------------------------------------------------------------------------------------


def test_message_session_says_no_access_is_not_found_and_where_ids_come_from(tools) -> None:
    tool = tools["message_session"]
    refusals = tool.description.split("Refusals:", 1)[1]

    assert "no such session in your workspace" not in tool.description
    assert "not-found (a session in a workspace you hold no access to" in refusals
    session_id = tool.args_schema["properties"]["session_id"]["description"]
    assert "list_my_sessions" in session_id and "find_sessions" in session_id and "full access" in session_id


def test_restart_says_the_tools_refuse_a_cancelled_session(tools) -> None:
    description = tools["restart_workspace_session"].description

    assert "completed, failed, or cancelled" not in description
    assert "Works for completed and failed sessions" in description
    assert "a cancelled one is refused by the tools (type=conflict)" in description
    assert "only a person's Restart over REST" in description


def test_get_workspace_session_says_where_to_read_the_state_and_the_park_key(tools) -> None:
    description = tools["get_workspace_session"].description

    assert "no ``session_state`` and no ``parked_event_key``" in description
    tail = description.split("no ``parked_event_key``", 1)[1]
    assert "find_sessions" in tail and "list_my_sessions" in tail


# ---- round 2 of #917, item 7: the workspace create and delete -----------------------------------------------------------------------------


def test_create_workspace_says_the_row_starts_pending_and_is_usable_at_once(tools) -> None:
    from primer.api.registries.workspace_registry import NEW_WORKSPACE_PHASE

    description = tools["create_workspace"].description
    assert f"starts ``phase={NEW_WORKSPACE_PHASE}``" in description
    assert "usable at once" in description
    assert "``running`` at its first good probe" in description


def test_delete_workspace_says_what_a_failed_delete_answers_and_leaves(tools) -> None:
    description = tools["delete_workspace"].description

    assert "``phase=terminating``" in description
    failed = description.split("``type=backend-error``", 1)[1]
    assert "``phase=failed``" in failed and "failure_reason" in failed and "workspace_lost" in failed
    assert "delete it again" in failed
    assert "cannot be resolved" in failed and "put back" in failed
