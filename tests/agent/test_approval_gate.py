"""Tests for evaluate_approval_gate — three strategies + fail-closed."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from matrix.agent.approval import (
    ApprovalContext,
    ApprovalVerdict,
    evaluate_approval_gate,
)
from matrix.model.tool_approval import (
    LlmApprovalConfig,
    PolicyApprovalConfig,
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)


def _ctx() -> ApprovalContext:
    return ApprovalContext(
        tool_name="shell_exec",
        toolset_id="_system",
        arguments={"cmd": "rm -rf /tmp/x"},
        agent_id="agt",
        session_id="sess",
        chat_id=None,
        requested_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_required_always_trips():
    policy = ToolApprovalPolicy(
        id="r", toolset_id="_system", tool_name="shell_exec",
        approval=RequiredApprovalConfig(),
    )
    v = await evaluate_approval_gate(
        policy=policy, context=_ctx(), provider_registry=None,
    )
    assert v.required is True


@pytest.mark.asyncio
async def test_policy_returns_required_with_reason():
    policy = ToolApprovalPolicy(
        id="p", toolset_id="_system", tool_name="shell_exec",
        approval=PolicyApprovalConfig(
            policy=(
                "package matrix.tool_approval\n"
                "default required := false\n"
                "default reason := \"\"\n"
                "required if input.arguments.cmd == \"rm -rf /tmp/x\"\n"
                "reason := \"destructive shell command\" if "
                "input.arguments.cmd == \"rm -rf /tmp/x\"\n"
            ),
        ),
    )
    v = await evaluate_approval_gate(
        policy=policy, context=_ctx(), provider_registry=None,
    )
    assert v.required is True
    assert "destructive" in (v.reason or "")


@pytest.mark.asyncio
async def test_policy_returns_not_required():
    policy = ToolApprovalPolicy(
        id="p", toolset_id="_system", tool_name="shell_exec",
        approval=PolicyApprovalConfig(
            policy=(
                "package matrix.tool_approval\n"
                "default required := false\n"
            ),
        ),
    )
    v = await evaluate_approval_gate(
        policy=policy, context=_ctx(), provider_registry=None,
    )
    assert v.required is False


@pytest.mark.asyncio
async def test_policy_compile_failure_fails_closed():
    policy = ToolApprovalPolicy(
        id="p", toolset_id="_system", tool_name="shell_exec",
        approval=PolicyApprovalConfig(policy="this is not rego at all"),
    )
    v = await evaluate_approval_gate(
        policy=policy, context=_ctx(), provider_registry=None,
    )
    assert v.required is True
    assert "policy" in (v.reason or "").lower()


@pytest.mark.asyncio
async def test_llm_judge_returns_structured_verdict(monkeypatch):
    policy = ToolApprovalPolicy(
        id="l", toolset_id="_system", tool_name="shell_exec",
        approval=LlmApprovalConfig(
            provider_id="prov", model="m", prompt="judge!",
        ),
    )

    async def _fake_judge(*args, **kwargs) -> dict[str, Any]:
        return {"required": True, "reason": "looks risky"}

    monkeypatch.setattr(
        "matrix.agent.approval._dispatch_llm_judge", _fake_judge,
    )
    v = await evaluate_approval_gate(
        policy=policy, context=_ctx(),
        provider_registry=object(),
    )
    assert v.required is True
    assert v.reason == "looks risky"


@pytest.mark.asyncio
async def test_llm_judge_failure_fails_closed(monkeypatch):
    policy = ToolApprovalPolicy(
        id="l", toolset_id="_system", tool_name="shell_exec",
        approval=LlmApprovalConfig(
            provider_id="prov", model="m", prompt="judge!",
        ),
    )

    async def _boom(*args, **kwargs) -> dict[str, Any]:
        raise RuntimeError("provider down")

    monkeypatch.setattr(
        "matrix.agent.approval._dispatch_llm_judge", _boom,
    )
    v = await evaluate_approval_gate(
        policy=policy, context=_ctx(),
        provider_registry=object(),
    )
    assert v.required is True
    assert "unavailable" in (v.reason or "").lower()
