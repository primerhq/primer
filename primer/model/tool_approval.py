"""ToolApprovalPolicy entity + discriminated ApprovalConfig union.

Stored entity keyed by ``(toolset_id, tool_name)``. The
``ApprovalResolver`` (primer.agent.approval) looks up the policy at
dispatch time; if one exists and ``enabled=True``, the configured
evaluator decides whether the call requires operator approval.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, ClassVar, Literal, Union

from pydantic import BaseModel, Field

from primer.model.common import Identifiable


class ApprovalType(str, Enum):
    """Approval-gate strategies."""

    REQUIRED = "required"
    POLICY = "policy"
    LLM = "llm"


class RequiredApprovalConfig(BaseModel):
    """``type=required`` — gate trips unconditionally."""

    type: Literal[ApprovalType.REQUIRED] = Field(default=ApprovalType.REQUIRED)


class PolicyApprovalConfig(BaseModel):
    """``type=policy`` — evaluate a Rego policy against the call context."""

    type: Literal[ApprovalType.POLICY] = Field(default=ApprovalType.POLICY)
    policy: str = Field(
        ...,
        min_length=1,
        description=(
            "Rego policy source. Must evaluate to a result document "
            "with a boolean ``required`` key and an optional string "
            "``reason`` key."
        ),
    )


class LlmApprovalConfig(BaseModel):
    """``type=llm`` — ask an LLM judge for the approval verdict."""

    type: Literal[ApprovalType.LLM] = Field(default=ApprovalType.LLM)
    provider_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Id of an existing ``LLMProvider`` row whose adapter "
            "answers the judge call."
        ),
    )
    model: str = Field(
        ...,
        min_length=1,
        description=(
            "Model name as published by the provider's ``models`` "
            "list. Validated at policy create/update time."
        ),
    )
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=16000,
        description=(
            "System prompt the judge receives. The call context is "
            "appended as the user message."
        ),
    )


ApprovalConfig = Annotated[
    Union[RequiredApprovalConfig, PolicyApprovalConfig, LlmApprovalConfig],
    Field(discriminator="type"),
]


class ToolApprovalPolicy(Identifiable):
    """Operator-configured approval gate for one ``(toolset_id, tool_name)``."""

    _id_prefix: ClassVar[str] = "tool-approval-policy"

    toolset_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Toolset id the policy applies to. May be a reserved "
            "internal toolset (``_system``, ``_workspaces``, "
            "``_misc``, ``_search``, ``web``) or a user-created "
            "Toolset row's id."
        ),
    )
    tool_name: str = Field(
        ...,
        min_length=1,
        description=(
            "Bare tool name as registered in the provider catalogue."
        ),
    )
    enabled: bool = Field(
        default=True,
        description=(
            "When false the policy is stored but skipped at "
            "evaluation time."
        ),
    )
    approval: ApprovalConfig = Field(
        ...,
        description="Approval strategy and its config.",
    )
    timeout_seconds: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Optional per-policy timeout. None falls back to the "
            "global yield cap."
        ),
    )


__all__ = [
    "ApprovalConfig",
    "ApprovalType",
    "LlmApprovalConfig",
    "PolicyApprovalConfig",
    "RequiredApprovalConfig",
    "ToolApprovalPolicy",
]
