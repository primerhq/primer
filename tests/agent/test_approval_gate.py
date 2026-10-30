"""Tests for evaluate_approval_gate: three strategies + fail-closed."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from primer.agent.approval import (
    ApprovalContext,
    ApprovalVerdict,
    evaluate_approval_gate,
)
from primer.model.tool_approval import (
    LlmApprovalConfig,
    PolicyApprovalConfig,
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)


def _ctx() -> ApprovalContext:
    return ApprovalContext(
        tool_name="shell_exec",
        toolset_id="system",
        arguments={"cmd": "rm -rf /tmp/x"},
        agent_id="agt",
        session_id="sess",
        chat_id=None,
        requested_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_required_always_trips():
    policy = ToolApprovalPolicy(
        id="r", toolset_id="system", tool_name="shell_exec",
        approval=RequiredApprovalConfig(),
    )
    v = await evaluate_approval_gate(
        policy=policy, context=_ctx(), provider_registry=None,
    )
    assert v.required is True


@pytest.mark.asyncio
async def test_policy_returns_required_with_reason():
    policy = ToolApprovalPolicy(
        id="p", toolset_id="system", tool_name="shell_exec",
        approval=PolicyApprovalConfig(
            policy=(
                "package primer.tool_approval\n"
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
        id="p", toolset_id="system", tool_name="shell_exec",
        approval=PolicyApprovalConfig(
            policy=(
                "package primer.tool_approval\n"
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
        id="p", toolset_id="system", tool_name="shell_exec",
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
        id="l", toolset_id="system", tool_name="shell_exec",
        approval=LlmApprovalConfig(
            provider_id="prov", model="m", prompt="judge!",
        ),
    )

    async def _fake_judge(*args, **kwargs) -> dict[str, Any]:
        return {"required": True, "reason": "looks risky"}

    monkeypatch.setattr(
        "primer.agent.approval._dispatch_llm_judge", _fake_judge,
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
        id="l", toolset_id="system", tool_name="shell_exec",
        approval=LlmApprovalConfig(
            provider_id="prov", model="m", prompt="judge!",
        ),
    )

    async def _boom(*args, **kwargs) -> dict[str, Any]:
        raise RuntimeError("provider down")

    monkeypatch.setattr(
        "primer.agent.approval._dispatch_llm_judge", _boom,
    )
    v = await evaluate_approval_gate(
        policy=policy, context=_ctx(),
        provider_registry=object(),
    )
    assert v.required is True
    assert "unavailable" in (v.reason or "").lower()


@pytest.mark.asyncio
async def test_llm_judge_resolves_through_the_profile_seam(monkeypatch):
    """D-A3-02: the judge must enter the house resolution seam
    (resolve_llm) instead of calling get_llm directly, so the profile's
    API-level config applies to the verdict the way it applies to a turn.
    The policy still names (provider_id, model) - that is what the
    console writes - so the seam is entered via the ModelProfile row that
    names that pair, and the verdict must flow through unchanged."""
    from primer.model.model_profile import ModelProfile, ModelProfileConfig
    from primer.model.storage import OffsetPageResponse
    from primer.model_profile import ResolvedModel

    row = ModelProfile(
        id="mp-judge", description="the judge profile", kind="single",
        provider_id="prov", model_name="m", context_length=4096,
    )

    class _Store:
        async def get(self, row_id):
            return row if row_id == row.id else None

        async def find(self, predicate, page):
            return OffsetPageResponse(offset=0, length=1, items=[row])

    class _SP:
        def get_storage(self, model_cls):
            return _Store()

    class _Registry:
        _sp = _SP()

        async def get_llm(self, provider_id):
            raise AssertionError(
                "the judge must resolve through the profile seam, not get_llm"
            )

    calls: list[str] = []

    class _Judge:
        async def judge_structured(self, **kwargs) -> dict[str, Any]:
            return {"required": False, "reason": "judged through the seam"}

    async def _spy(sp, registry, *, default_profile_id, override_profile_id=None):
        calls.append(default_profile_id)
        return _Judge(), ResolvedModel(
            profile_id=default_profile_id, provider_id="prov",
            model_name="m", context_length=4096,
            config=ModelProfileConfig(),
        )

    monkeypatch.setattr("primer.model_profile.resolve_llm", _spy)

    policy = ToolApprovalPolicy(
        id="l", toolset_id="system", tool_name="shell_exec",
        approval=LlmApprovalConfig(
            provider_id="prov", model="m", prompt="judge!",
        ),
    )
    v = await evaluate_approval_gate(
        policy=policy, context=_ctx(), provider_registry=_Registry(),
    )
    assert calls == ["mp-judge"], "the judge must resolve the profile row through resolve_llm"
    assert v.required is False
    assert v.reason == "judged through the seam"
