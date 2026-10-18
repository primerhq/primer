"""An approval policy gates a tool whichever spelling of its toolset id a call uses (pivot P1 follow-up, security).

Four built-in toolsets lost an underscore prefix (``_system`` is now ``system``) and the registry still resolves the old id to the same provider
(``canonical_toolset_id``), so ``call_tool(toolset_id="_system", ...)`` runs the very tool ``call_tool(toolset_id="system", ...)`` does. ``call_tool`` looked the
approval policy up under the id it was SPELLED with: a policy on ``system::list_llm_providers`` parked the second call and let the first run ungated (a reviewer
reproduced exactly that). The same held for an agent whose ``tools`` list, or a graph ``tool_call`` node whose ``tool_id``, carries the old prefix (the tool manager
keys the gate on the toolset id as written), and for a policy written FOR the old id (``ToolApprovalPolicy.toolset_id`` takes any string, and its field description
names the underscore ids) against a call spelled the new way.

For every alias the registry accepts (the table is READ, and a test fails when a new alias has no tool here to call):

* the reviewer's repro, exactly;
* ``call_tool``: a policy on the current id gates a call spelled either way, and a policy stored under the retired id gates it too; the park names the CURRENT id (the
  card, the audit record and the resume read it); a gated call with no session behind it fails closed; the policy's own Rego sees the current id;
* the tool manager (an agent whose tools name the toolset by either id, with the policy under either), which a graph ``tool_call`` node dispatches through
  (tests/graph/test_workspace_executor.py);
* ``describe_tool`` reports the same gate whichever spelling it is asked for (tests/toolset/test_describe_tool.py).
"""

from __future__ import annotations

import json

import pytest

from primer.agent.approval import ApprovalResolver
from primer.agent.tool_manager import ToolExecutionManager
from primer.common.toolset_ids import TOOLSET_ID_ALIASES
from primer.model.chat import ToolCallPart
from primer.model.tool_approval import PolicyApprovalConfig, RequiredApprovalConfig, ToolApprovalPolicy
from primer.model.yield_ import ToolContext, YieldToWorker
from primer.toolset.system import build_system_toolset
from tests.agent.test_inner_dispatch import ADMIN, _Session
from tests.mcp.test_required_role_completeness import reserved_provider_registry  # noqa: F401  (the fixture: a registry with every built-in toolset wired)
from tests.toolset._scripted_toolset import Registry, Scripted, scripted_tool

# (retired id, current id), read from the registry's table.
ALIASES = sorted(TOOLSET_ID_ALIASES.items())

# A tool of each current toolset to call: user role, so it needs neither an admin nor a session to be refused or allowed for the wrong reason, and callable with no
# arguments (so an ungated call runs; a gated one parks before its arguments are read anyway).
TOOL = {"system": "list_agents", "workspaces": "list_workspaces", "misc": "get_datetime", "search": "find"}

# The same two spellings for the parametrised tests below.
SPELLINGS = ["current", "retired"]


def _spell(spelling: str, alias: str, current: str) -> str:
    return current if spelling == "current" else alias


@pytest.fixture
def sp(fake_storage_provider):
    return fake_storage_provider


@pytest.fixture
def search() -> Scripted:
    """The stored ``search`` toolset (a row, not a built-in; its retired id is ``_search``): one user-role tool, which records every call it is asked to run."""
    return Scripted([scripted_tool("find", toolset_id="search")], roles={"find": "user"})


@pytest.fixture
def registry(reserved_provider_registry, search):  # noqa: F811 - the re-exported fixture
    """Resolves every toolset the way the app's registry does: the built-ins, the stored ``search``, and each retired id through ``canonical_toolset_id``."""
    return Registry({"search": search}, reserved_provider_registry)


@pytest.fixture
def resolver(sp) -> ApprovalResolver:
    return ApprovalResolver(storage=sp.get_storage(ToolApprovalPolicy))


@pytest.fixture
def system_toolset(sp, registry, resolver):
    """The system toolset ``call_tool`` lives in, over that registry and ONE resolver (as the app hands the toolset the resolver the agent loop gates with)."""
    return build_system_toolset(storage_provider=sp, provider_registry=registry, approval_resolver=resolver)


def _ctx(call_id: str = "call-1") -> ToolContext:
    return ToolContext(tool_call_id=call_id, session_id="sess-1", workspace_id="ws-1", initiated_by=ADMIN)


def _policy(policy_id: str, toolset_id: str, tool_name: str, **fields) -> ToolApprovalPolicy:
    return ToolApprovalPolicy(id=policy_id, toolset_id=toolset_id, tool_name=tool_name, approval=fields.pop("approval", RequiredApprovalConfig()), **fields)


def _sees_toolset(toolset_id: str) -> PolicyApprovalConfig:
    """A conditional gate that trips only when the context it is evaluated on names ``toolset_id``."""
    return PolicyApprovalConfig(policy=f'package primer.tool_approval\ndefault required := false\nrequired if input.toolset_id == "{toolset_id}"\n')


def _call_tool_arguments(spelling: str, tool_name: str) -> dict:
    return {"toolset_id": spelling, "tool_name": tool_name, "arguments": {}}


async def _parks(system_toolset, spelling: str, tool_name: str):
    """The park of ``call_tool(toolset_id=spelling, tool_name=...)``; a call that is NOT parked for approval fails the test."""
    with pytest.raises(YieldToWorker) as parked:
        await system_toolset.call(tool_name="call_tool", ctx=_ctx(), arguments=_call_tool_arguments(spelling, tool_name))
    assert parked.value.yielded.tool_name == "_approval"
    return parked.value.yielded


def test_every_alias_has_a_tool_here():
    """A new alias in the registry's table needs a row in ``TOOL``, or its bypass would go untested."""
    assert set(TOOL) == set(TOOLSET_ID_ALIASES.values())


# ---- the reviewer's repro --------------------------------------------------------------------------------------------------------------------------


async def test_a_policy_on_system_list_llm_providers_gates_the_underscore_spelling_too(system_toolset, sp):
    await sp.get_storage(ToolApprovalPolicy).create(_policy("tap-1", "system", "list_llm_providers"))

    for spelling in ("system", "_system"):
        parked = await _parks(system_toolset, spelling, "list_llm_providers")
        assert parked.resume_metadata["policy_id"] == "tap-1", spelling


# ---- call_tool, every alias, the policy and the call spelled either way ---------------------------------------------------------------------------


@pytest.mark.parametrize(("alias", "current"), ALIASES)
@pytest.mark.parametrize("policy_spelling", SPELLINGS)
@pytest.mark.parametrize("call_spelling", SPELLINGS)
async def test_call_tool_is_gated_whichever_spelling_the_policy_and_the_call_use(system_toolset, sp, search, alias, current, policy_spelling, call_spelling):
    tool = TOOL[current]
    await sp.get_storage(ToolApprovalPolicy).create(_policy("tap-1", _spell(policy_spelling, alias, current), tool))

    parked = await _parks(system_toolset, _spell(call_spelling, alias, current), tool)

    meta = parked.resume_metadata
    assert meta["policy_id"] == "tap-1" and meta["original_call"]["name"] == tool
    # The park names the toolset as the platform knows it: the card, the audit record and the resume read this, and a spelling the caller chose is not theirs to keep.
    assert meta["via_call_tool"] == {"toolset_id": current, "tool_name": tool}
    assert search.calls == [], "the inner tool ran although its call was parked for approval"


@pytest.mark.parametrize(("alias", "current"), ALIASES)
async def test_an_ungated_tool_still_runs_under_either_spelling(system_toolset, sp, alias, current):
    """The control: gating one tool must not gate its neighbours, whichever way the toolset is spelled."""
    await sp.get_storage(ToolApprovalPolicy).create(_policy("tap-1", current, "some_other_tool"))

    for spelling in (current, alias):
        result = await system_toolset.call(tool_name="call_tool", ctx=_ctx(), arguments=_call_tool_arguments(spelling, TOOL[current]))
        assert not result.is_error, (spelling, result.output)


@pytest.mark.parametrize(("alias", "current"), ALIASES)
async def test_a_policy_of_another_toolset_gates_neither_spelling(system_toolset, sp, alias, current):
    """The same tool name in another toolset (``crud::list_llm_providers``-style) is another policy: aliasing must not widen a gate across toolsets."""
    await sp.get_storage(ToolApprovalPolicy).create(_policy("tap-1", "rec", TOOL[current]))

    for spelling in (current, alias):
        result = await system_toolset.call(tool_name="call_tool", ctx=_ctx(), arguments=_call_tool_arguments(spelling, TOOL[current]))
        assert not result.is_error, (spelling, result.output)


@pytest.mark.parametrize(("alias", "current"), ALIASES)
@pytest.mark.parametrize("call_spelling", SPELLINGS)
async def test_a_policy_stored_under_the_retired_id_sees_the_current_id_in_its_own_rego(system_toolset, sp, alias, current, call_spelling):
    """A conditional gate reads the context's ``toolset_id``. Stored under the retired id (the one spelling the old lookup found for a call spelled that way), it must
    still be evaluated on the CURRENT id, whichever way the call spelled it."""
    tool = TOOL[current]
    await sp.get_storage(ToolApprovalPolicy).create(_policy("tap-1", alias, tool, approval=_sees_toolset(current)))

    await _parks(system_toolset, _spell(call_spelling, alias, current), tool)


@pytest.mark.parametrize(("alias", "current"), ALIASES)
@pytest.mark.parametrize("call_spelling", SPELLINGS)
async def test_a_gated_call_with_no_session_fails_closed_under_either_spelling(system_toolset, sp, alias, current, call_spelling):
    """No session or chat to park onto: the call must not run, spelled either way."""
    tool = TOOL[current]
    await sp.get_storage(ToolApprovalPolicy).create(_policy("tap-1", current, tool))

    result = await system_toolset.call(tool_name="call_tool", arguments=_call_tool_arguments(_spell(call_spelling, alias, current), tool))

    assert result.is_error and json.loads(result.output)["type"] == "approval-required"


# ---- the tool manager: an agent whose tools name the toolset by either id ------------------------------------------------------------------------------


async def _manager(registry, resolver, key: str, current: str) -> tuple[ToolExecutionManager, str]:
    """The manager the worker builds for an agent whose ``tools`` name the toolset as ``key`` (its current id, or a retired prefix): the provider the registry resolves
    it to, keyed by the id as written."""
    scoped = f"{key}__{TOOL[current]}"
    manager = ToolExecutionManager.for_workspace(
        toolset_providers={key: await registry.get_toolset(key)},
        session=_Session("sess-1", "ws-1"),
        tools=[scoped],
        approval_resolver=resolver,
        initiated_by=ADMIN,
    )
    return manager, scoped


@pytest.mark.parametrize(("alias", "current"), ALIASES)
@pytest.mark.parametrize("policy_spelling", SPELLINGS)
@pytest.mark.parametrize("agent_spelling", SPELLINGS)
async def test_the_tool_manager_gates_a_tool_whichever_spelling_the_agent_and_the_policy_use(
    registry, resolver, sp, search, alias, current, policy_spelling, agent_spelling,
):
    await sp.get_storage(ToolApprovalPolicy).create(_policy("tap-1", _spell(policy_spelling, alias, current), TOOL[current]))
    manager, scoped = await _manager(registry, resolver, _spell(agent_spelling, alias, current), current)

    with pytest.raises(YieldToWorker) as parked:
        await manager.execute(ToolCallPart(id="c1", name=scoped, arguments={}))

    meta = parked.value.yielded.resume_metadata
    assert parked.value.yielded.tool_name == "_approval" and meta["policy_id"] == "tap-1" and meta["original_call"]["name"] == scoped
    assert search.calls == [], "the tool ran although its call was parked for approval"


@pytest.mark.parametrize(("alias", "current"), ALIASES)
@pytest.mark.parametrize("agent_spelling", SPELLINGS)
async def test_the_tool_managers_context_names_the_current_id_to_a_conditional_gate(registry, resolver, sp, alias, current, agent_spelling):
    await sp.get_storage(ToolApprovalPolicy).create(_policy("tap-1", alias, TOOL[current], approval=_sees_toolset(current)))
    manager, scoped = await _manager(registry, resolver, _spell(agent_spelling, alias, current), current)

    with pytest.raises(YieldToWorker):
        await manager.execute(ToolCallPart(id="c1", name=scoped, arguments={}))


@pytest.mark.parametrize(("alias", "current"), ALIASES)
@pytest.mark.parametrize("agent_spelling", SPELLINGS)
async def test_the_tool_manager_still_runs_an_ungated_tool_under_either_spelling(registry, resolver, search, alias, current, agent_spelling):
    """The control: the retired prefix keeps working (the registry resolves it), and a tool nobody gated runs."""
    manager, scoped = await _manager(registry, resolver, _spell(agent_spelling, alias, current), current)

    part = await manager.execute(ToolCallPart(id="c1", name=scoped, arguments={}))

    assert part.id == "c1" and part.error is False, part.output
    if current == "search":
        assert search.calls == [("find", {})]
