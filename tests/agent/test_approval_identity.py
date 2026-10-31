"""The approval input carries the run's composite identity ``{user, actor}`` (access-control P4 Task 1, spec 6.3).

Every gate that builds an approval input (the tool manager's direct gate, ``system__call_tool`` with and without a context, and the MCP endpoint)
hands Rego and the LLM judge the same ``identity`` document for the same caller, so a policy on ``input.identity`` decides a call alike through
every door (Review Focus 6).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import primer.agent.approval as approval_mod
from primer.agent.approval import ApprovalResolver
from primer.agent.rego import RegoVerdict
from primer.agent.tool_manager import ToolExecutionManager
from primer.mcp.dispatch import _enforce_approval_gate
from primer.mcp.server import current_actor
from primer.model.chat import ToolCallPart
from primer.model.principal import Principal
from primer.model.tool_approval import LlmApprovalConfig, PolicyApprovalConfig, ToolApprovalPolicy
from primer.model.user import User
from primer.model.yield_ import YieldToWorker
from tests.agent.test_inner_dispatch import USER, _Session
from tests.toolset.test_call_tool_door import pr, recorder, sp, system_toolset  # noqa: F401 - fixtures

NOW = datetime.now(UTC)

USER_IDENTITY = {"user": {"id": "u-1", "name": "user", "role": "user"}, "actor": {"kind": "agent", "id": "agent-builder"}}


class _CaptureRego:
    """Stands in for the module's Rego evaluator: records every input document and never gates."""

    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    def evaluate(self, *, policy_id: str, policy_text: str, input: dict[str, Any]) -> RegoVerdict:  # noqa: A002 - the evaluator's keyword
        self.inputs.append(input)
        return RegoVerdict(required=False)


@pytest.fixture
def captured(monkeypatch) -> _CaptureRego:
    cap = _CaptureRego()
    monkeypatch.setattr(approval_mod, "_rego_evaluator", cap)
    return cap


def _policy(approval: Any, *, toolset_id: str = "rec", tool_name: str = "user_x") -> ToolApprovalPolicy:
    return ToolApprovalPolicy(id="p-id", toolset_id=toolset_id, tool_name=tool_name, approval=approval)


def _manager(toolsets: dict[str, Any], sp, *, initiated_by=USER, registry: Any = None) -> ToolExecutionManager:  # noqa: ANN001 - fixtures
    return ToolExecutionManager.for_workspace(
        toolset_providers=toolsets,
        session=_Session("s-1", "ws-1", agent_id="agent-builder"),
        approval_resolver=ApprovalResolver(storage=sp.get_storage(ToolApprovalPolicy)),
        provider_registry=registry,
        tools=[f"{tid}__{name}" for tid, name in (("rec", "user_x"), ("system", "call_tool"))],
        initiated_by=initiated_by,
    )


_ON_BOB_AS_BUILDER = (
    "package primer.tool_approval\n"
    "default required := false\n"
    "required if {\n"
    '    input.identity.user.id == "u-1"\n'
    '    input.identity.user.role == "user"\n'
    '    input.identity.actor == {"kind": "agent", "id": "agent-builder"}\n'
    "}\n"
)


async def test_rego_input_carries_identity(sp, recorder):  # noqa: F811 - fixtures
    """A real Rego policy on ``input.identity`` gates the run's person acting through the run's agent, and nobody else."""
    await sp.get_storage(ToolApprovalPolicy).create(_policy(PolicyApprovalConfig(policy=_ON_BOB_AS_BUILDER)))
    with pytest.raises(YieldToWorker):
        await _manager({"rec": recorder}, sp).execute(ToolCallPart(id="c1", name="rec__user_x", arguments={}))
    assert recorder.calls == []

    other = USER.model_copy(update={"id": "u-2", "display": "other"})
    part = await _manager({"rec": recorder}, sp, initiated_by=other).execute(ToolCallPart(id="c2", name="rec__user_x", arguments={}))
    assert part.error is False, part.output
    assert [name for name, *_ in recorder.calls] == ["user_x"], "another person's call is not gated"

    bare = approval_mod.ApprovalContext(
        tool_name="t", toolset_id="ts", arguments={}, agent_id=None, session_id=None, chat_id=None, requested_at=NOW,
    )
    assert bare.to_input_doc()["identity"] is None, "a context built without one still carries the key"


async def test_judge_input_carries_identity(sp, recorder):  # noqa: F811 - fixtures
    """The LLM judge's user message is the same input document, identity included."""
    seen: list[str] = []

    class _Judge:
        async def judge_structured(self, *, model: str, system_prompt: str, user_message: str, response_schema: dict) -> dict:
            seen.append(user_message)
            return {"required": False}

    class _Registry:
        async def get_llm(self, provider_id: str) -> _Judge:
            return _Judge()

    await sp.get_storage(ToolApprovalPolicy).create(_policy(LlmApprovalConfig(provider_id="llm-1", model="m", prompt="judge")))
    part = await _manager({"rec": recorder}, sp, registry=_Registry()).execute(ToolCallPart(id="c1", name="rec__user_x", arguments={}))
    assert part.error is False, part.output
    (message,) = seen
    assert json.loads(message)["identity"] == USER_IDENTITY


async def test_call_tool_gate_sees_the_direct_identity(sp, recorder, system_toolset, captured):  # noqa: F811 - fixtures
    """``call_tool`` hands its gate the identity the direct gate builds for the same run (the ctx's person and actor)."""
    await sp.get_storage(ToolApprovalPolicy).create(_policy(PolicyApprovalConfig(policy="package primer.tool_approval\n")))
    manager = _manager({"rec": recorder, "system": system_toolset}, sp)
    direct = await manager.execute(ToolCallPart(id="c1", name="rec__user_x", arguments={}))
    door = await manager.execute(ToolCallPart(
        id="c2", name="system__call_tool", arguments={"toolset_id": "rec", "tool_name": "user_x", "arguments": {}},
    ))
    assert direct.error is False and door.error is False, (direct.output, door.output)
    assert [doc["identity"] for doc in captured.inputs] == [USER_IDENTITY, USER_IDENTITY]


async def test_mcp_gate_identity_is_the_token_user(sp, recorder, system_toolset, captured):  # noqa: F811 - fixtures
    """Over MCP the person is the token's owner, named by their username (read from the User row, not the token's name), and the actor is that user
    acting directly. The MCP endpoint's gate and ``call_tool`` with no context (its MCP door) agree."""
    await sp.get_storage(User).create(User(id="u-ada", username="ada", created_at=NOW, role="user"))
    await sp.get_storage(ToolApprovalPolicy).create(_policy(PolicyApprovalConfig(policy="package primer.tool_approval\n")))
    token = Principal(type="api_token", id="tok-1", display="ci-token", role="user", source="internal", user_id="u-ada")
    expected = {"user": {"id": "u-ada", "name": "ada", "role": "user"}, "actor": {"kind": "user", "id": "u-ada"}}

    deps = SimpleNamespace(approval_resolver=ApprovalResolver(storage=sp.get_storage(ToolApprovalPolicy)), provider_registry=SimpleNamespace(storage_provider=sp))
    await _enforce_approval_gate(scoped_id="rec__user_x", toolset_id="rec", bare_name="user_x", arguments={}, principal="tok-1", deps=deps, actor=token)

    reset = current_actor.set(token)
    try:
        r = await system_toolset.call(tool_name="call_tool", arguments={"toolset_id": "rec", "tool_name": "user_x", "arguments": {}})
    finally:
        current_actor.reset(reset)
    assert not r.is_error, r.output
    assert [doc["identity"] for doc in captured.inputs] == [expected, expected]
