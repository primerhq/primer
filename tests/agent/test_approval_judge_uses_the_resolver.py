"""RED probe (D-A3-02): the LLM approval judge must go through the house model-profile resolution.

Every other LLM call site resolves its adapter through the profile seam
(``primer.model_profile.resolve_llm``: a "single" profile resolves to the
same adapter ``get_llm`` would build, and an "aggregated" one gains the
failover the direct call never had). The approval judge
(``primer.agent.approval._dispatch_llm_judge``) still calls
``provider_registry.get_llm(policy.provider_id)`` direct, bypassing the one
selection contract. This probe pins that the judge call reaches the seam: a
spy on ``resolve_llm`` must be consulted with the profile the policy's
(provider, model) names, and the verdict must still come through the
resolved judge.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

import primer.model_profile as model_profile
from primer.agent.approval import ApprovalContext, evaluate_approval_gate
from primer.model.model_profile import ModelProfile
from primer.model.provider import SqliteConfig
from primer.model.tool_approval import LlmApprovalConfig, ToolApprovalPolicy
from primer.storage.sqlite import SqliteStorageProvider


class _Judge:
    """A structured judge that records the model it was asked with."""

    def __init__(self) -> None:
        self.models: list[str] = []

    async def judge_structured(self, *, model: str, **_kwargs) -> dict:
        self.models.append(model)
        return {"required": False, "reason": "safe"}


class _Registry:
    """The minimal registry surface the judge path uses."""

    def __init__(self, judge: _Judge, sp) -> None:
        self._judge = judge
        # The fix reads the storage provider off the registry (house
        # precedent tool_approval.py) to enter the seam; pin it.
        self._sp = sp

    async def get_llm(self, provider_id: str) -> _Judge:
        assert provider_id == "p1"
        return self._judge


@pytest.mark.asyncio
async def test_the_llm_judge_resolves_through_the_profile_seam(tmp_path, monkeypatch) -> None:
    storage = SqliteStorageProvider(SqliteConfig(path=tmp_path / "judge.sqlite"))
    await storage.initialize()
    try:
        await storage.get_storage(ModelProfile).create(
            ModelProfile(
                id="judge-prof",
                description="the judge profile",
                provider_id="p1",
                model_name="judge",
                context_length=8192,
            ),
        )
        judge = _Judge()
        registry = _Registry(judge, storage)

        real_resolve = model_profile.resolve_llm
        calls: list[dict] = []

        async def _spy(sp, pr, **kwargs):
            calls.append(kwargs)
            return await real_resolve(sp, pr, **kwargs)

        monkeypatch.setattr(model_profile, "resolve_llm", _spy)

        policy = ToolApprovalPolicy(
            toolset_id="system",
            tool_name="shell_exec",
            approval=LlmApprovalConfig(
                provider_id="p1", model="judge", prompt="Is this safe?"
            ),
        )
        context = ApprovalContext(
            tool_name="shell_exec",
            toolset_id="system",
            arguments={"cmd": "ls"},
            agent_id="agt",
            session_id="sess",
            chat_id=None,
            requested_at=datetime.now(UTC),
        )
        # The seam takes the storage provider once the fix lands; until then
        # the gate has no storage parameter and the judge cannot be routed
        # through the resolver at all.
        extra = (
            {"storage_provider": storage}
            if "storage_provider" in inspect.signature(evaluate_approval_gate).parameters
            else {}
        )
        verdict = await evaluate_approval_gate(
            policy=policy, context=context, provider_registry=registry, **extra,
        )

        assert calls, "the judge call bypassed the profile seam: resolve_llm was never consulted"
        assert calls[0]["default_profile_id"] == "judge-prof", (
            f"the judge must resolve the profile the policy's (provider, model) names, got {calls[0]}"
        )
        assert verdict.required is False, "the verdict must still come through the resolved judge"
        assert judge.models == ["judge"], "the judge ran on the resolved model"
    finally:
        await storage.aclose()
