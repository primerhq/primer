"""The approval park stamps WHICH arguments its card may show (design note 01a11cd3-66b0, rulings D1, D2 and D5, slice 1).

``GET /v1/yields/pending`` is polled by every open console and reads only the parked blob: it has no catalogue and no policy cache. So the effective allowlist is
RESOLVED where the tool and the policy are both in hand, in ``ToolExecutionManager`` just before the park, and stamped into the metadata by the one shared builder
(``approval_resume_metadata``):

    resume_metadata["preview"] = {"paths": [...], "source": "policy" | "tool" | "default"}

Precedence (D1): the operator's policy list, else the tool's own declaration, else the default rule (D2): the top-level arguments whose schema is a closed set
(boolean, integer, number, null, enum, const), taken from the tool's schema NOW so the route needs none. A policy list of ``[]`` and a tool declaration of ``()``
are declarations: show no value.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from primer.agent.approval import ApprovalResolver, ApprovalVerdict, approval_resume_metadata, resolve_preview
from primer.agent.tool_manager import ToolExecutionManager, _workspace_tool_descriptor
from primer.model.chat import ToolCallPart, ToolCallResult
from primer.model.principal import PrincipalRef
from primer.model.tool_approval import RequiredApprovalConfig, ToolApprovalPolicy
from primer.model.yield_ import YieldToWorker
from primer.toolset._describe import make_tool

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "mode": {"enum": ["read", "write"], "type": "string"},
        "force": {"type": "boolean"},
        "note": {"type": "string"},
    },
}


def _tool(tool_id: str, **declared):
    return make_tool(id=tool_id, toolset_id="pv", purpose="Do it.", when="Use when testing.", args_schema=SCHEMA, examples=[], **declared)


class _Provider:
    def __init__(self, *tools) -> None:
        self._tools = tools

    async def list_tools(self, *, principal: str | None = None):
        for tool in self._tools:
            yield tool

    def required_role(self, tool_name: str) -> str:
        return "admin"

    async def call(self, *, tool_name: str, arguments: dict[str, Any], principal: str | None = None, ctx=None) -> ToolCallResult:
        return ToolCallResult(output="ok", is_error=False)


class _Resolver(ApprovalResolver):
    def __init__(self, policies: list[ToolApprovalPolicy]) -> None:
        self._policies = policies
        self._ttl = 60.0
        self._cache = {}

    async def find(self, *, toolset_id, tool_name):
        return next((p for p in self._policies if (p.toolset_id, p.tool_name) == (toolset_id, tool_name)), None)


def _policy(tool_name: str, **fields) -> ToolApprovalPolicy:
    return ToolApprovalPolicy(id=f"p-{tool_name}", toolset_id="pv", tool_name=tool_name, approval=RequiredApprovalConfig(), **fields)


async def _park(tool_name: str, policy: ToolApprovalPolicy, *, tools=None) -> dict:
    declared = tools if tools is not None else (
        _tool("declared", preview_args=("path", "mode")), _tool("plain"), _tool("hidden", preview_args=()),
    )
    manager = ToolExecutionManager(toolset_providers={"pv": _Provider(*declared)}, initiated_by=PrincipalRef.system())  # type: ignore[arg-type]
    manager._approval_resolver = _Resolver([policy])  # noqa: SLF001
    with pytest.raises(YieldToWorker) as parked:
        await manager.execute(ToolCallPart(id="c1", name=f"pv__{tool_name}", arguments={"path": "/a", "mode": "read", "force": True, "note": "n"}))
    return parked.value.yielded.resume_metadata


@pytest.mark.asyncio
async def test_the_tools_own_declaration_is_stamped() -> None:
    meta = await _park("declared", _policy("declared"))

    assert meta["preview"] == {"paths": ["path", "mode"], "source": "tool"}


@pytest.mark.asyncio
async def test_a_tool_that_declares_nothing_gets_the_closed_set_of_its_schema() -> None:
    meta = await _park("plain", _policy("plain"))

    assert meta["preview"] == {"paths": ["mode", "force"], "source": "default"}, "mode is an enum and force a boolean; path and note are free text"


@pytest.mark.asyncio
async def test_the_policy_list_wins_over_the_tools_declaration() -> None:
    meta = await _park("declared", _policy("declared", preview_args=["note"]))

    assert meta["preview"] == {"paths": ["note"], "source": "policy"}


@pytest.mark.asyncio
async def test_the_policy_list_wins_over_the_default_too() -> None:
    meta = await _park("plain", _policy("plain", preview_args=["path"]))

    assert meta["preview"] == {"paths": ["path"], "source": "policy"}


@pytest.mark.asyncio
async def test_an_empty_policy_list_shows_no_value_and_beats_a_declaration() -> None:
    meta = await _park("declared", _policy("declared", preview_args=[]))

    assert meta["preview"] == {"paths": [], "source": "policy"}


@pytest.mark.asyncio
async def test_an_empty_tool_declaration_is_a_declaration_not_the_default() -> None:
    meta = await _park("hidden", _policy("hidden"))

    assert meta["preview"] == {"paths": [], "source": "tool"}


@pytest.mark.asyncio
async def test_the_stamp_is_beside_the_rest_of_the_park_not_instead_of_it() -> None:
    meta = await _park("declared", _policy("declared"))

    assert meta["policy_id"] == "p-declared" and meta["original_call"]["name"] == "pv__declared"
    assert meta["original_call"]["arguments"]["note"] == "n", "the parked call keeps ALL its arguments: the stamp only limits what a card draws"


@pytest.mark.asyncio
async def test_a_tool_the_agent_may_not_list_still_has_a_descriptor_for_the_stamp() -> None:
    """The routing table is built before the agent's allowlist filters the visible catalogue (a tool the agent cannot list can still be reached through
    ``call_tool``), and so is the descriptor map the stamp is resolved from."""
    manager = ToolExecutionManager(
        toolset_providers={"pv": _Provider(_tool("declared", preview_args=("path",)))},  # type: ignore[arg-type]
        initiated_by=PrincipalRef.system(),
        tools=["pv__someone_else"],
    )

    visible = await manager.list_tools()

    assert [tool.id for tool in visible] == [], "the agent's allowlist hides it from the catalogue"
    assert manager._descriptors["pv__declared"].preview_args == ("path",)  # noqa: SLF001


# ---- the shared builder and the pure resolution ----------------------------------------------------------------------------------------------------------------


def test_the_shared_builder_carries_the_stamp_and_omits_the_key_when_given_none() -> None:
    verdict = ApprovalVerdict(required=True, reason="r")
    call = {"id": "c", "name": "n", "arguments": {}}
    with_stamp = approval_resume_metadata(policy=_policy("declared"), verdict=verdict, original_call=call, preview={"paths": ["a"], "source": "tool"})
    without = approval_resume_metadata(policy=_policy("declared"), verdict=verdict, original_call=call)

    assert with_stamp["preview"] == {"paths": ["a"], "source": "tool"}
    assert "preview" not in without, "a park site that passes nothing leaves the row unstamped (it then takes the default rule at read time)"


def test_resolve_preview_with_no_descriptor_hides_everything_by_default() -> None:
    assert resolve_preview(policy=_policy("x"), tool=None) == {"paths": [], "source": "default"}


def test_resolve_preview_does_not_mutate_the_policy_or_the_tool() -> None:
    policy, tool = _policy("x", preview_args=["a"]), _tool("declared", preview_args=("path",))

    stamp = resolve_preview(policy=policy, tool=tool)
    stamp["paths"].append("b")

    assert policy.preview_args == ["a"] and tool.preview_args == ("path",)


# ---- workspace tools declare the same way ----------------------------------------------------------------------------------------------------------------------


class _Args(BaseModel):
    path: str
    force: bool = False


class _FakeWorkspaceTool:
    id = "fake"
    description = "d"
    examples: list = []
    interruptible = True
    preview_args = ("path",)

    def parameters(self):
        return _Args


def test_a_workspace_tools_class_declaration_reaches_its_descriptor() -> None:
    descriptor = _workspace_tool_descriptor(_FakeWorkspaceTool(), scoped_id="workspace__fake")

    assert descriptor.preview_args == ("path",)


def test_a_workspace_tool_that_declares_nothing_has_no_declaration_on_its_descriptor() -> None:
    class _Plain:
        id = "plain"
        description = "d"
        examples: list = []
        interruptible = True        # no preview_args attribute at all: a duck-typed tool, as the neighbouring descriptor code already tolerates

        def parameters(self):
            return _Args

    assert _workspace_tool_descriptor(_Plain(), scoped_id="workspace__plain").preview_args is None
