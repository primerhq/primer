"""Validation tests for the ToolApprovalPolicy + ApprovalConfig models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from matrix.model.tool_approval import (
    ApprovalType,
    LlmApprovalConfig,
    PolicyApprovalConfig,
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)


def test_approval_type_string_values():
    assert ApprovalType.REQUIRED.value == "required"
    assert ApprovalType.POLICY.value == "policy"
    assert ApprovalType.LLM.value == "llm"


def test_required_minimal():
    cfg = RequiredApprovalConfig()
    assert cfg.type == ApprovalType.REQUIRED


def test_policy_requires_non_empty_policy():
    with pytest.raises(ValidationError):
        PolicyApprovalConfig(policy="")


def test_llm_requires_provider_model_prompt():
    cfg = LlmApprovalConfig(
        provider_id="p", model="m", prompt="judge this",
    )
    assert cfg.type == ApprovalType.LLM
    assert cfg.provider_id == "p"


def test_policy_model_round_trip():
    row = ToolApprovalPolicy(
        id="approve-shell",
        toolset_id="system",
        tool_name="shell_exec",
        approval=PolicyApprovalConfig(policy="package m\nrequired := false"),
    )
    assert row.enabled is True
    assert row.approval.type == ApprovalType.POLICY
    dumped = row.model_dump(mode="json")
    reloaded = ToolApprovalPolicy.model_validate(dumped)
    assert reloaded.approval.type == ApprovalType.POLICY


def test_policy_discriminator_picks_required():
    row = ToolApprovalPolicy(
        id="r",
        toolset_id="system",
        tool_name="t",
        approval={"type": "required"},
    )
    assert isinstance(row.approval, RequiredApprovalConfig)


def test_policy_discriminator_picks_llm():
    row = ToolApprovalPolicy(
        id="r",
        toolset_id="system",
        tool_name="t",
        approval={
            "type": "llm",
            "provider_id": "p",
            "model": "m",
            "prompt": "judge",
        },
    )
    assert isinstance(row.approval, LlmApprovalConfig)
    assert row.approval.provider_id == "p"


def test_timeout_seconds_optional_positive():
    row = ToolApprovalPolicy(
        id="r",
        toolset_id="system",
        tool_name="t",
        approval=RequiredApprovalConfig(),
        timeout_seconds=120.0,
    )
    assert row.timeout_seconds == 120.0
    with pytest.raises(ValidationError):
        ToolApprovalPolicy(
            id="r",
            toolset_id="system",
            tool_name="t",
            approval=RequiredApprovalConfig(),
            timeout_seconds=0.0,
        )


def test_disabled_flag_optional_default_true():
    row = ToolApprovalPolicy(
        id="r",
        toolset_id="system",
        tool_name="t",
        approval=RequiredApprovalConfig(),
    )
    assert row.enabled is True
