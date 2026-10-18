"""``system__describe_tool`` returns one tool's schema, role, live gate and a call example (pivot P1, task 6).

An agent that carries only ``call_tool`` needs a cheap way to learn the exact arguments of one tool: ``list_toolset_tools`` dumps a whole toolset. This tool answers
for ONE ``(toolset_id, tool_name)`` from the tool catalog (``primer.toolset.tool_catalog``), so the built-in toolsets, a user-made toolset, the session file tools
(``workspace``) and the client tool (``client``) are described the same way. What is pinned here:

* the output keys are exactly the contract (the wiki's reference pages and the agents read them), and each value is the catalog's, not a second opinion;
* the call example names the right door: ``call_tool`` for a toolset, a direct call for the two scopes ``call_tool`` cannot reach;
* the gate is the LIVE one, read from the same ``ApprovalResolver`` the gates use (an operator's edit shows; a disabled row does not), never the shipped default,
  and under whichever spelling of the toolset it is asked for (``_misc`` is ``misc``);
* the refusals: an unknown toolset, an unknown tool (naming the toolset's tools), a toolset that will not list, bad arguments; an OAuth consent request is not
  swallowed;
* it is a plain read: role user, no pause, no workspace (so it can be exposed over MCP), and it works without a run context.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from primer.agent.approval import ApprovalResolver
from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.bootstrap.seed import crud_policy_id, ensure_crud_approval_policies
from primer.common.toolset_ids import TOOLSET_ID_ALIASES
from primer.mcp.safety import is_exposable
from primer.model.chat import Tool, ToolCallResult
from primer.model.except_ import AuthRequiredError
from primer.model.principal import PrincipalRef
from primer.model.tool_approval import PolicyApprovalConfig, RequiredApprovalConfig, ToolApprovalPolicy
from primer.model.yield_ import ToolContext
from primer.toolset._system_crud import _describe_tool_tool, _live_approval_gate
from primer.toolset.system import build_system_toolset
from primer.toolset.tool_catalog import ToolEntry, collect_tool_entries
from tests.mcp.test_required_role_completeness import reserved_provider_registry  # noqa: F401  (the fixture: a registry with every built-in toolset wired)
from tests.toolset._scripted_toolset import Registry, Scripted, scripted_tool

USER = PrincipalRef(type="user", id="u-1", display="user", role="user", source="local")

KEYS = {
    "scoped_id", "toolset_id", "tool_name", "description", "args_schema", "examples", "required_role", "pauses_turn", "needs_workspace", "call", "approval", "reference",
}


@pytest.fixture
def sp(fake_storage_provider):
    return fake_storage_provider


@pytest.fixture
def rec() -> Scripted:
    """A user-made toolset: ``go`` is user-role, ``wait`` pauses, ``locked`` declares nothing (so it fails closed to admin)."""
    return Scripted([scripted_tool("go"), scripted_tool("wait"), scripted_tool("locked")], roles={"go": "user", "wait": "user"}, yielding=frozenset({"wait"}))


@pytest.fixture
def system_toolset(sp, reserved_provider_registry, rec):  # noqa: F811 - the re-exported fixture
    """The system toolset over a registry that knows every built-in toolset (as the app wires it) plus the user-made ``rec``."""
    return build_system_toolset(storage_provider=sp, provider_registry=Registry({"rec": rec}, reserved_provider_registry))


def _ctx(principal_ref: PrincipalRef | None = USER) -> ToolContext:
    return ToolContext(tool_call_id="call-1", session_id="sess-1", workspace_id="ws-1", initiated_by=principal_ref)


async def _describe(toolset: Any, toolset_id: str, tool_name: str, *, ctx: ToolContext | None = None) -> ToolCallResult:
    return await toolset.call(tool_name="describe_tool", ctx=ctx or _ctx(), arguments={"toolset_id": toolset_id, "tool_name": tool_name})


async def _described(toolset: Any, toolset_id: str, tool_name: str) -> dict[str, Any]:
    result = await _describe(toolset, toolset_id, tool_name)
    assert not result.is_error, result.output
    return json.loads(result.output)


async def _tool(toolset: Any, name: str) -> Tool:
    async for tool in toolset.list_tools():
        if tool.id == name:
            return tool
    raise AssertionError(f"the system toolset has no {name!r}")


def _policy(policy_id: str, toolset_id: str, tool_name: str, **fields: Any) -> ToolApprovalPolicy:
    return ToolApprovalPolicy(id=policy_id, toolset_id=toolset_id, tool_name=tool_name, approval=fields.pop("approval", RequiredApprovalConfig()), **fields)


# ---- what it returns -----------------------------------------------------------------------------------------------------------------------------------


async def test_describe_a_gated_crud_tool(system_toolset, sp):
    await ensure_crud_approval_policies(sp)
    out = await _described(system_toolset, "crud", "create_agent")
    assert out["scoped_id"] == "crud__create_agent" and out["call"]["via"] == "call_tool"
    assert out["approval"] == {"gated": True, "type": "required", "policy_id": crud_policy_id("create_agent")}
    assert out["reference"] == "reference/tools/crud/create_agent"
    Draft202012Validator(out["args_schema"]).validate(out["call"]["example"]["arguments"])


async def test_describe_a_session_file_tool_shows_a_direct_call(system_toolset):
    out = await _described(system_toolset, "workspace", "read")
    assert out["call"] == {"via": "direct", "example": {"name": "workspace__read", "arguments": {"path": "README.md"}}}
    assert (out["required_role"], out["needs_workspace"], out["approval"]) == ("user", True, {"gated": False})


async def test_describe_the_client_tool_shows_a_direct_call(system_toolset):
    out = await _described(system_toolset, "client", "open_file")
    assert out["call"] == {"via": "direct", "example": {"name": "client__open_file", "arguments": {"path": "config.yaml"}}}
    assert (out["scoped_id"], out["reference"], out["required_role"]) == ("client__open_file", "reference/tools/client/open_file", "user")


async def test_the_output_keys_are_exactly_the_contract(system_toolset):
    out = await _described(system_toolset, "misc", "get_datetime")
    assert set(out) == KEYS
    assert set(out["call"]) == {"via", "example"} and set(out["call"]["example"]) == {"toolset_id", "tool_name", "arguments"}
    assert out["call"]["example"] == {"toolset_id": "misc", "tool_name": "get_datetime", "arguments": out["examples"][0]["args"]}
    assert all(set(example) == {"args", "returns", "note"} for example in out["examples"])
    direct = await _described(system_toolset, "workspace", "write")
    assert set(direct) == KEYS and set(direct["call"]["example"]) == {"name", "arguments"}


async def test_the_values_are_the_listed_tools_own(system_toolset, reserved_provider_registry):  # noqa: F811
    listed = {t.id: t async for t in (await reserved_provider_registry.get_toolset("misc")).list_tools()}["get_datetime"]
    out = await _described(system_toolset, "misc", "get_datetime")
    assert out["description"] == listed.description and out["args_schema"] == listed.args_schema and out["tool_name"] == "get_datetime"
    assert out["examples"] == [{"args": e.args, "returns": e.returns, "note": e.note} for e in listed.examples]
    assert out["pauses_turn"] is False and out["needs_workspace"] is False and out["required_role"] == "user"
    json.dumps(out)  # a plain JSON document


async def test_a_pausing_tool_and_an_admin_tool_say_so(system_toolset):
    sleeps = await _described(system_toolset, "workspace_ext", "sleep")
    assert (sleeps["pauses_turn"], sleeps["required_role"]) == (True, "user")
    waits = await _described(system_toolset, "system", "wait_for_event")
    assert (waits["pauses_turn"], waits["needs_workspace"]) == (True, True)
    assert (await _described(system_toolset, "system", "get_llm_provider"))["required_role"] == "admin"


async def test_every_builtin_tool_is_described_as_the_catalog_has_it(system_toolset, reserved_provider_registry):  # noqa: F811
    """All of them, so a tool whose entry the handler cannot build (a non-JSON value, a lookup that drifts) is found here and not by an agent.

    The entries come from the registry the tool resolves through (a toolset's optional tools exist only when its collaborators are wired).
    """
    providers = {toolset_id: await reserved_provider_registry.get_toolset(toolset_id) for toolset_id in RESERVED_TOOLSET_IDS}
    entries = await collect_tool_entries(providers)
    assert len(entries) > 150, "the catalog came back too small for this check to mean anything"
    broken: list[str] = []
    for entry in entries:
        result = await _describe(system_toolset, entry.toolset_id, entry.tool.id)
        if result.is_error:
            broken.append(f"{entry.scoped_id}: {result.output[:120]}")
            continue
        out = json.loads(result.output)
        if (out["scoped_id"], out["required_role"], out["pauses_turn"], out["needs_workspace"]) != (
            entry.scoped_id, entry.required_role, entry.pauses_turn, entry.needs_workspace,
        ):
            broken.append(f"{entry.scoped_id}: the answer differs from the catalog's entry")
        if entry.tool.examples:
            problems = list(Draft202012Validator(out["args_schema"]).iter_errors(out["call"]["example"]["arguments"]))
            if problems:
                broken.append(f"{entry.scoped_id}: its call example does not validate: {problems[0].message[:100]}")
    assert not broken, "\n".join(broken)


async def test_a_user_made_toolset_is_described_by_its_provider(system_toolset, rec):
    out = await _described(system_toolset, "rec", "go")
    assert (out["scoped_id"], out["required_role"], out["pauses_turn"], out["call"]["via"]) == ("rec__go", "user", False, "call_tool")
    assert out["call"]["example"] == {"toolset_id": "rec", "tool_name": "go", "arguments": {}}, "a tool with no example gets an empty argument object"
    assert (await _described(system_toolset, "rec", "wait"))["pauses_turn"] is True
    assert (await _described(system_toolset, "rec", "locked"))["required_role"] == "admin", "an undeclared tool fails closed, as call_tool ranks it"
    assert rec.calls == [], "describing a tool never runs it"


# ---- the live gate -------------------------------------------------------------------------------------------------------------------------------------


async def test_the_gate_is_the_live_policy_not_the_shipped_default(system_toolset, sp):
    store = sp.get_storage(ToolApprovalPolicy)
    await store.create(_policy("tap-uuid", "misc", "uuid_v4", approval=PolicyApprovalConfig(policy="package p\nrequired := true\n")))
    assert (await _described(system_toolset, "misc", "uuid_v4"))["approval"] == {"gated": True, "type": "policy", "policy_id": "tap-uuid"}

    await ensure_crud_approval_policies(sp)
    current = await store.get(crud_policy_id("update_agent"))
    await store.update(current.model_copy(update={"enabled": False}))
    assert (await _described(system_toolset, "crud", "update_agent"))["approval"] == {"gated": False}, "a disabled policy gates nothing"
    assert (await _described(system_toolset, "crud", "create_agent"))["approval"]["gated"] is True


async def test_a_session_file_tool_with_a_policy_is_gated(system_toolset, sp):
    """The tool manager gates the session tools by ``(workspace, tool)``; so does the answer."""
    await sp.get_storage(ToolApprovalPolicy).create(_policy("tap-write", "workspace", "write"))
    assert (await _described(system_toolset, "workspace", "write"))["approval"] == {"gated": True, "type": "required", "policy_id": "tap-write"}
    assert (await _described(system_toolset, "workspace", "read"))["approval"] == {"gated": False}


async def test_it_asks_the_resolver_the_gates_use(sp, reserved_provider_registry):  # noqa: F811
    """One resolver, one cache: an operator's ``/invalidate`` reaches this answer exactly as it reaches the gate (and a verdict cached before it is as stale)."""
    resolver = ApprovalResolver(storage=sp.get_storage(ToolApprovalPolicy), cache_ttl_seconds=600.0)
    toolset = build_system_toolset(storage_provider=sp, provider_registry=reserved_provider_registry, approval_resolver=resolver)
    assert (await _described(toolset, "misc", "uuid_v4"))["approval"] == {"gated": False}
    await sp.get_storage(ToolApprovalPolicy).create(_policy("tap-uuid", "misc", "uuid_v4"))
    assert (await _described(toolset, "misc", "uuid_v4"))["approval"] == {"gated": False}, "the cached verdict, as the gate would use it"
    resolver.invalidate()
    assert (await _described(toolset, "misc", "uuid_v4"))["approval"] == {"gated": True, "type": "required", "policy_id": "tap-uuid"}


async def test_with_no_resolver_wired_nothing_is_gated(sp, reserved_provider_registry):  # noqa: F811
    await sp.get_storage(ToolApprovalPolicy).create(_policy("tap-uuid", "misc", "uuid_v4"))
    _, (_, handler) = _describe_tool_tool(reserved_provider_registry, None)
    out = json.loads((await handler({"toolset_id": "misc", "tool_name": "uuid_v4"})).output)
    assert out["approval"] == {"gated": False}


# ---- one toolset, several spellings ----------------------------------------------------------------------------------------------------------------------
#
# ``_misc`` is the retired id of ``misc`` and the registry runs the same provider for both. A policy on one spelling gates the other (the approval-gate bypass
# through a toolset-id alias, see tests/toolset/test_approval_gate_toolset_aliases.py), so the gate this tool reports must not depend on the spelling it was asked about
# or the one a descriptor carries.

ALIASES = sorted(TOOLSET_ID_ALIASES.items())
TOOL = {"system": "list_agents", "workspaces": "list_workspaces", "misc": "get_datetime", "search": "find"}


@pytest.mark.parametrize(("alias", "current"), ALIASES)
async def test_a_tool_is_described_the_same_whichever_spelling_of_its_toolset_is_asked_for(sp, reserved_provider_registry, alias, current):  # noqa: F811
    search = Scripted([scripted_tool("find", toolset_id="search")], roles={"find": "user"})
    toolset = build_system_toolset(storage_provider=sp, provider_registry=Registry({"search": search}, reserved_provider_registry))
    tool = TOOL[current]
    await sp.get_storage(ToolApprovalPolicy).create(_policy("tap-1", current, tool))

    by_current, by_alias = await _described(toolset, current, tool), await _described(toolset, alias, tool)

    assert by_alias == by_current, "the answer depends on the spelling of the toolset"
    assert by_alias["toolset_id"] == current and by_alias["scoped_id"] == f"{current}__{tool}" and by_alias["reference"] == f"reference/tools/{current}/{tool}"
    assert by_alias["approval"] == {"gated": True, "type": "required", "policy_id": "tap-1"}


@pytest.mark.parametrize(("alias", "current"), ALIASES)
@pytest.mark.parametrize("policy_spelling", ["current", "retired"])
@pytest.mark.parametrize("entry_spelling", ["current", "retired"])
async def test_the_live_gate_is_the_same_whichever_spelling_names_the_toolset(sp, alias, current, policy_spelling, entry_spelling):
    """``_live_approval_gate`` asks the resolver for the entry's ``(toolset_id, tool)``: a descriptor that carries a retired id, or a policy stored under one, is the
    same toolset, so the answer is the same."""
    spelled = {"current": current, "retired": alias}
    await sp.get_storage(ToolApprovalPolicy).create(_policy("tap-1", spelled[policy_spelling], "go"))
    resolver = ApprovalResolver(storage=sp.get_storage(ToolApprovalPolicy))
    entry = ToolEntry(
        toolset_id=spelled[entry_spelling], tool=scripted_tool("go", toolset_id=spelled[entry_spelling]),
        required_role="user", pauses_turn=False, needs_workspace=False, default_gate=None,
    )

    assert await _live_approval_gate(resolver, entry) == {"gated": True, "type": "required", "policy_id": "tap-1"}


# ---- the refusals --------------------------------------------------------------------------------------------------------------------------------------


async def test_an_unknown_tool_lists_the_toolsets_tools(system_toolset):
    r = await system_toolset.call(tool_name="describe_tool", ctx=_ctx(), arguments={"toolset_id": "misc", "tool_name": "nope"})
    body = json.loads(r.output)
    assert r.is_error and body["type"] == "not-found" and "get_datetime" in body["message"]
    assert body["message"].startswith("no tool 'nope' in toolset 'misc'; its tools: ")


async def test_an_unknown_tool_names_at_most_thirty_of_them_in_a_fixed_order(system_toolset):
    body = json.loads((await _describe(system_toolset, "system", "nope")).output)
    prefix = "no tool 'nope' in toolset 'system'; its tools: "
    assert body["type"] == "not-found" and body["message"].startswith(prefix)
    names = body["message"].removeprefix(prefix).split(", ")
    assert len(names) == 30 and names == sorted(names)


async def test_an_unknown_toolset_is_not_found(system_toolset):
    r = await _describe(system_toolset, "nope", "x")
    assert r.is_error and json.loads(r.output) == {"type": "not-found", "message": "no toolset 'nope'"}


async def test_a_toolset_that_will_not_list_is_a_provider_error(sp, reserved_provider_registry):  # noqa: F811
    broken = Scripted([scripted_tool("go")], fail_after=0)
    toolset = build_system_toolset(storage_provider=sp, provider_registry=Registry({"broken": broken}, reserved_provider_registry))
    r = await _describe(toolset, "broken", "go")
    body = json.loads(r.output)
    assert r.is_error and body["type"] == "provider-error" and "broken" in body["message"]


async def test_an_oauth_consent_request_is_not_swallowed(sp, reserved_provider_registry):  # noqa: F811
    """It propagates, so the run is parked for the consent exactly as ``list_toolset_tools`` parks it, and the person gets the URL."""
    needs_consent = Scripted([scripted_tool("go")], auth=True)
    toolset = build_system_toolset(storage_provider=sp, provider_registry=Registry({"mcp-x": needs_consent}, reserved_provider_registry))
    with pytest.raises(AuthRequiredError):
        await _describe(toolset, "mcp-x", "go")


async def test_the_listing_is_made_as_the_principal_the_runs_context_carries(reserved_provider_registry):  # noqa: F811
    """A toolset with per-user state (an MCP toolset's OAuth token cache) answers for the person the run is for, so the listing gets the principal STRING on the
    run's context, as ``call_tool`` hands its inner provider; no context, or one with none, lists anonymously.

    The handler is called directly with a stand-in context: only the ``principal`` attribute is the contract here.
    """
    rec = Scripted([scripted_tool("go")], roles={"go": "user"})
    _, (_, handler) = _describe_tool_tool(Registry({"rec": rec}, reserved_provider_registry), None)
    arguments = {"toolset_id": "rec", "tool_name": "go"}
    assert not (await handler(arguments, ctx=SimpleNamespace(principal="alice"))).is_error
    await handler(arguments)
    await handler(arguments, ctx=SimpleNamespace())
    assert rec.listed_as == ["alice", None, None]


@pytest.mark.parametrize("arguments", [{}, {"toolset_id": "misc"}, {"tool_name": "uuid_v4"}, {"toolset_id": "", "tool_name": "x"}, {"toolset_id": "misc", "tool_name": ""}])
async def test_bad_arguments_are_a_validation_error(system_toolset, arguments):
    r = await system_toolset.call(tool_name="describe_tool", ctx=_ctx(), arguments=arguments)
    assert r.is_error and json.loads(r.output)["type"] == "validation-error"


# ---- what kind of tool it is ---------------------------------------------------------------------------------------------------------------------------


async def test_describe_tool_is_role_user(system_toolset):
    tool = await _tool(system_toolset, "describe_tool")
    assert tool.required_role == "user"
    assert set(tool.args_schema["properties"]) == {"toolset_id", "tool_name"} and set(tool.args_schema["required"]) == {"toolset_id", "tool_name"}


async def test_it_is_a_plain_read_that_can_be_exposed_over_mcp(system_toolset):
    """No pause and no workspace: the two things MCP cannot carry. (Role user: the MCP gate then lets a non-admin caller read it.)"""
    tool = await _tool(system_toolset, "describe_tool")
    assert tool.yields is False and tool.requires_workspace is False and tool.interruptible is True
    assert is_exposable(tool, provider=system_toolset) == (True, None)


async def test_it_works_without_a_run_context(system_toolset):
    """The MCP path (``invoke_one``) calls with no ``ToolContext``."""
    r = await system_toolset.call(tool_name="describe_tool", arguments={"toolset_id": "misc", "tool_name": "get_datetime"})
    assert not r.is_error and json.loads(r.output)["scoped_id"] == "misc__get_datetime"


async def test_its_own_examples_describe_real_tools(system_toolset):
    """The examples in its description are calls to itself that work."""
    for example in (await _tool(system_toolset, "describe_tool")).examples:
        r = await system_toolset.call(tool_name="describe_tool", ctx=_ctx(), arguments=example.args)
        assert not r.is_error, (example.args, r.output)
