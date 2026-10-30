"""Running or binding an agent or graph needs read access on it (security review finding 3; spec 6.2 read = see it, run it)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import SecretStr

from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.except_ import ValidationError
from primer.model.principal import PrincipalRef
from primer.model.workspace import Workspace as WorkspaceRow
from primer.model.workspace import WorkspaceRuntimeMeta
from primer.model.workspace_session import AgentSessionBinding
from primer.model.yield_ import ToolContext
from primer.workspace.session_factory import SessionFactoryDeps, start_workspace_session

from tests.workspace.test_session_factory_initiated_by import (  # noqa: F401 - fixtures
    _FakeClaimEngine,
    _FakeLiveWorkspace,
    _FakeScheduler,
    _FakeWorkspaceRegistry,
)

ALICE = PrincipalRef(type="user", id="alice", display="alice", role="user", source="local", user_id="alice")
BOB = PrincipalRef(type="user", id="bob", display="bob", role="user", source="local", user_id="bob")


async def _seed(sp, agent_access: Access) -> SessionFactoryDeps:
    await sp.get_storage(WorkspaceRow).create(
        WorkspaceRow(
            id="ws-1", template_id="tpl-1", provider_id="local-1", created_at=datetime.now(timezone.utc),
            runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959/", token=SecretStr("t")),
        )
    )
    await sp.get_storage(Agent).create(
        Agent(id="ag-private", description="x", model=AgentModel(profile_id="p")).model_copy(update={"access": agent_access})
    )
    return SessionFactoryDeps(
        storage_provider=sp, claim_engine=_FakeClaimEngine(), scheduler=_FakeScheduler(),
        workspace_registry=_FakeWorkspaceRegistry(_FakeLiveWorkspace()),
    )


async def _start(deps, who: PrincipalRef):
    return await start_workspace_session(
        workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag-private"), initial_instructions=None,
        graph_input=None, auto_start=False, metadata={}, parent_session_id=None, deps=deps, initiated_by=who,
    )


@pytest.mark.asyncio
async def test_a_session_cannot_start_on_an_agent_the_person_cannot_read(fake_storage_provider):
    deps = await _seed(fake_storage_provider, Access(owners=["alice"]))
    with pytest.raises(ValidationError) as refused:
        await _start(deps, BOB)
    assert str(refused.value) == "Agent 'ag-private' does not exist"  # exactly like a missing agent
    assert (await _start(deps, ALICE)).id


@pytest.mark.asyncio
async def test_a_read_grant_lets_the_person_run_it(fake_storage_provider):
    deps = await _seed(fake_storage_provider, Access(owners=["alice"], grants={"bob": "read"}))
    assert (await _start(deps, BOB)).id


@pytest.mark.asyncio
async def test_invoke_agent_refuses_an_unreadable_agent(fake_storage_provider):
    await fake_storage_provider.get_storage(Agent).create(
        Agent(id="ag-hidden", description="x", model=AgentModel(profile_id="p")).model_copy(
            update={"access": Access(owners=["alice"])}
        )
    )
    from primer.toolset import system as system_mod

    handler = _find_handler(system_mod, fake_storage_provider, "invoke_agent")
    ctx = ToolContext(tool_call_id="c1", session_id="s1", workspace_id="ws-1", initiated_by=BOB)
    result = await handler({"agent_id": "ag-hidden", "prompt": "hi"}, ctx=ctx)
    assert result.is_error and json.loads(result.output)["type"] == "not-found"


def _find_handler(system_mod, sp, name: str):
    provider = system_mod.build_system_toolset(storage_provider=sp, provider_registry=None)
    _tool, handler = provider._registry[name]  # noqa: SLF001 - the test reaches the registered handler
    return handler
