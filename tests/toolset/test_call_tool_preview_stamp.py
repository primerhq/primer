"""``call_tool`` stamps the INNER tool's allowlist on its approval park (design note 01a11cd3-66b0, slice 1 condition (b)).

``system::call_tool`` runs any tool of any toolset by id. Two parks involve it:

1. A policy on the INNER tool: the handler parks for approval itself, with the INNER call as ``original_call`` (name and arguments) and ``via_call_tool``. The card
   must filter those arguments by the INNER tool's list, so the handler looks the inner tool up (the registry, under a clock) and resolves the stamp like the agent loop does
   (policy list, else the tool's declaration, else the closed-set default). An inner tool that cannot be found has no descriptor to take a schema from: every argument is
   withheld, and the stamp says ``default`` with no paths.
2. A policy on ``call_tool`` itself, gated by the tool manager: its arguments are ``{toolset_id, tool_name, arguments, principal}``. Its own declaration shows the first
   two; the inner ``arguments`` are shown by the inner tool's paths (``arguments.<path>``) when the manager holds the inner descriptor, and withheld otherwise. An operator's
   list on the ``call_tool`` policy is taken as it is.
"""

from __future__ import annotations

from typing import Any

import pytest

from primer.agent.approval import ApprovalResolver
from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import ToolCallPart, ToolCallResult
from primer.model.principal import PrincipalRef
from primer.model.tool_approval import RequiredApprovalConfig, ToolApprovalPolicy
from primer.model.yield_ import ToolContext, YieldToWorker
from primer.toolset._describe import make_tool
from primer.toolset._system_crud import _call_tool_tool

SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "mode": {"enum": ["read", "write"], "type": "string"}, "force": {"type": "boolean"}, "note": {"type": "string"}},
}
ARGS = {"path": "/a", "mode": "read", "force": True, "note": "free text"}


def _inner(tool_id: str = "inner", **declared):
    return make_tool(id=tool_id, toolset_id="pv", purpose="Do it.", when="Use when testing.", args_schema=SCHEMA, examples=[], **declared)


class _Provider:
    def __init__(self, *tools, fail: bool = False) -> None:
        self._tools, self._fail = tools, fail

    async def list_tools(self, *, principal=None):
        if self._fail:
            raise RuntimeError("unreachable")
        for tool in self._tools:
            yield tool

    def required_role(self, tool_name: str) -> str:
        return "user"

    async def call(self, *, tool_name: str, arguments: dict[str, Any], principal=None, ctx=None) -> ToolCallResult:
        return ToolCallResult(output="ran", is_error=False)


class _Registry:
    _sp = None

    def __init__(self, providers: dict[str, _Provider]) -> None:
        self._providers = providers

    async def get_toolset(self, toolset_id: str):
        if toolset_id not in self._providers:
            raise KeyError(toolset_id)
        return self._providers[toolset_id]


class _Resolver:
    def __init__(self, policy: ToolApprovalPolicy) -> None:
        self._policy = policy

    async def find(self, *, toolset_id, tool_name):
        return self._policy if (toolset_id, tool_name) == (self._policy.toolset_id, self._policy.tool_name) else None


def _policy(toolset_id: str, tool_name: str, **fields) -> ToolApprovalPolicy:
    return ToolApprovalPolicy(id="p-1", toolset_id=toolset_id, tool_name=tool_name, approval=RequiredApprovalConfig(), **fields)


async def _park_inner(providers: dict[str, _Provider], policy: ToolApprovalPolicy, *, tool_name: str = "inner") -> dict:
    _, (_, handler) = _call_tool_tool(_Registry(providers), _Resolver(policy))  # type: ignore[arg-type]
    ctx = ToolContext(tool_call_id="call-1", session_id="sess-1", workspace_id="ws-1", chat_id=None)
    with pytest.raises(YieldToWorker) as parked:
        await handler({"toolset_id": policy.toolset_id, "tool_name": tool_name, "arguments": ARGS}, ctx=ctx)
    return parked.value.yielded.resume_metadata


# ---- 1. a policy on the inner tool: the handler parks -----------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_inner_tools_own_declaration_is_stamped() -> None:
    meta = await _park_inner({"pv": _Provider(_inner(preview_args=("path", "mode")))}, _policy("pv", "inner"))

    assert meta["preview"] == {"paths": ["path", "mode"], "source": "tool"}
    assert meta["original_call"]["name"] == "inner" and meta["original_call"]["arguments"] == ARGS, "the parked call is the inner call, whole"
    assert meta["via_call_tool"]["toolset_id"] == "pv"


@pytest.mark.asyncio
async def test_an_inner_tool_that_declares_nothing_gets_the_closed_set_of_its_schema() -> None:
    meta = await _park_inner({"pv": _Provider(_inner())}, _policy("pv", "inner"))

    assert meta["preview"] == {"paths": ["mode", "force"], "source": "default"}


@pytest.mark.asyncio
async def test_the_operators_list_on_the_inner_policy_wins() -> None:
    meta = await _park_inner({"pv": _Provider(_inner(preview_args=("path",)))}, _policy("pv", "inner", preview_args=["note"]))

    assert meta["preview"] == {"paths": ["note"], "source": "policy"}


@pytest.mark.asyncio
async def test_an_inner_tool_the_toolset_does_not_list_has_every_argument_withheld() -> None:
    meta = await _park_inner({"pv": _Provider(_inner("other"))}, _policy("pv", "inner"))

    assert meta["preview"] == {"paths": [], "source": "default"}


@pytest.mark.asyncio
async def test_a_toolset_that_fails_to_list_has_every_argument_withheld_and_the_park_still_happens() -> None:
    meta = await _park_inner({"pv": _Provider(fail=True)}, _policy("pv", "inner"))

    assert meta["preview"] == {"paths": [], "source": "default"}
    assert meta["policy_id"] == "p-1"


@pytest.mark.asyncio
async def test_an_unknown_inner_toolset_has_every_argument_withheld() -> None:
    meta = await _park_inner({}, _policy("pv", "inner"))

    assert meta["preview"] == {"paths": [], "source": "default"}


# ---- the declaration of call_tool itself ------------------------------------------------------------------------------------------------------------------


def test_call_tool_declares_the_two_arguments_that_say_which_tool_it_runs() -> None:
    _, (tool, _) = _call_tool_tool(_Registry({}), None)  # type: ignore[arg-type]

    assert tool.preview_args == ("toolset_id", "tool_name"), "the inner arguments and the principal are not named: they are filtered by the inner tool's list or withheld"


# ---- 2. a policy on call_tool itself: the tool manager gates it -------------------------------------------------------------------------------------------


class _PoliciesOnly(ApprovalResolver):
    def __init__(self, policy: ToolApprovalPolicy) -> None:
        self._policy = policy
        self._ttl = 60.0
        self._cache = {}

    async def find(self, *, toolset_id, tool_name):
        return self._policy if (toolset_id, tool_name) == (self._policy.toolset_id, self._policy.tool_name) else None


_DECLARED = object()


def _outer_manager(policy: ToolApprovalPolicy, *, inner: Any = _DECLARED) -> ToolExecutionManager:
    """A manager holding the system toolset's ``call_tool`` and (unless ``inner`` is None) a toolset ``pv`` with the inner tool."""
    _, (call_tool, _) = _call_tool_tool(_Registry({}), None)  # type: ignore[arg-type]
    system = _Provider(call_tool.model_copy(update={"toolset_id": "system"}))
    providers: dict[str, Any] = {"system": system}
    if inner is not None:
        providers["pv"] = _Provider(_inner(preview_args=("path", "mode")) if inner is _DECLARED else inner)
    manager = ToolExecutionManager(toolset_providers=providers, initiated_by=PrincipalRef.system())  # type: ignore[arg-type]
    manager._approval_resolver = _PoliciesOnly(policy)  # noqa: SLF001
    return manager


async def _park_outer(manager: ToolExecutionManager, inner_tool: str = "inner") -> dict:
    call = ToolCallPart(
        id="c1", name="system__call_tool",
        arguments={"toolset_id": "pv", "tool_name": inner_tool, "arguments": ARGS, "principal": "someone"},
    )
    with pytest.raises(YieldToWorker) as parked:
        await manager.execute(call)
    return parked.value.yielded.resume_metadata


@pytest.mark.asyncio
async def test_a_policy_on_call_tool_shows_which_tool_and_the_inner_tools_own_paths() -> None:
    meta = await _park_outer(_outer_manager(_policy("system", "call_tool")))

    assert meta["preview"] == {"paths": ["toolset_id", "tool_name", "arguments.path", "arguments.mode"], "source": "tool"}
    assert meta["original_call"]["arguments"]["principal"] == "someone", "the parked call keeps everything; only the card is limited"


@pytest.mark.asyncio
async def test_an_inner_tool_the_manager_does_not_hold_has_its_arguments_withheld() -> None:
    meta = await _park_outer(_outer_manager(_policy("system", "call_tool"), inner=None))

    assert meta["preview"] == {"paths": ["toolset_id", "tool_name"], "source": "tool"}


@pytest.mark.asyncio
async def test_an_inner_tool_with_no_declaration_adds_only_its_closed_set_paths() -> None:
    meta = await _park_outer(_outer_manager(_policy("system", "call_tool"), inner=_inner()))

    assert meta["preview"] == {"paths": ["toolset_id", "tool_name", "arguments.mode", "arguments.force"], "source": "tool"}


@pytest.mark.asyncio
async def test_the_operators_list_on_the_call_tool_policy_is_taken_as_it_is() -> None:
    meta = await _park_outer(_outer_manager(_policy("system", "call_tool", preview_args=["toolset_id"])))

    assert meta["preview"] == {"paths": ["toolset_id"], "source": "policy"}
