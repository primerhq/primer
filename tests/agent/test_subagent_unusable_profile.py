"""A sub-agent spends the model as the person its parent run acts as (access-control P4 Task 4).

``invoke_agent`` answers a profile that person may not use with a tool error (``forbidden``), never by failing the parent session.
"""

from __future__ import annotations

import json

import pytest

import primer.agent.invoke as invoke_module
from primer.access.person import Person
from primer.authz import ProfileUnusable
from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.model_profile import ModelProfile
from primer.model.principal import PrincipalRef
from primer.model.yield_ import ToolContext
from primer.toolset.system import build_system_toolset
from tests.conftest import _FakeStorageProvider


class _Registry:
    async def get_llm(self, provider_id):
        return "llm"


async def _world() -> _FakeStorageProvider:
    sp = _FakeStorageProvider()
    await sp.get_storage(ModelProfile).create(
        ModelProfile(id="pro", description="d", provider_id="prov", model_name="m", context_length=1000, access=Access(grants={"owner": "read"}, rev=1))
    )
    await sp.get_storage(Agent).create(Agent.model_validate({"id": "sub", "description": "a sub", "model": {"profile_id": "pro"}, "access": {"public": True}}))
    return sp


async def test_the_resolution_is_checked_as_the_parents_person():
    sp = await _world()

    agent, llm, _ = await invoke_module._resolve_agent_runtime(
        "sub", storage_provider=sp, provider_registry=_Registry(), person=Person("owner", "user")
    )
    assert agent.id == "sub" and llm == "llm"
    with pytest.raises(ProfileUnusable):
        await invoke_module._resolve_agent_runtime("sub", storage_provider=sp, provider_registry=_Registry(), person=Person("peer", "user"))
    # No person (an internal caller with no identity) checks nothing, as before.
    assert (await invoke_module._resolve_agent_runtime("sub", storage_provider=sp, provider_registry=_Registry()))[0].id == "sub"


async def test_invoke_agent_answers_a_tool_error_not_a_failed_session():
    sp = await _world()
    from primer.api.registries import ProviderRegistry

    registry = ProviderRegistry(sp)
    toolset = build_system_toolset(storage_provider=sp, provider_registry=registry, workspace_registry=None)
    peer = PrincipalRef(type="user", id="peer", display="peer", role="user", source="local", user_id="peer")
    ctx = ToolContext(tool_call_id="c1", session_id="s1", workspace_id="w1", initiated_by=peer)

    result = await toolset.call(tool_name="invoke_agent", arguments={"agent_id": "sub", "prompt": "hi"}, ctx=ctx)

    assert result.is_error
    body = json.loads(result.output)
    assert body["type"] == "forbidden" and "'pro'" in body["message"] and "not available" in body["message"]
