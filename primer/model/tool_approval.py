"""ToolApprovalPolicy entity + discriminated ApprovalConfig union.

Stored entity keyed by ``(toolset_id, tool_name)``. The
``ApprovalResolver`` (primer.agent.approval) looks up the policy at
dispatch time; if one exists and ``enabled=True``, the configured
evaluator decides whether the call requires operator approval.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, ClassVar, Literal, Union

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
            "Model name the provider publishes, i.e. named by one of its "
            "``ModelProfile`` rows. The judge is a direct provider call "
            "rather than an agent turn, so it takes the bare name and no "
            "profile id. Validated at policy create/update time."
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
    RequiredApprovalConfig | PolicyApprovalConfig | LlmApprovalConfig,
    Field(discriminator="type"),
]


class ApproverSpec(BaseModel):
    """Who may decide a gated call (2026-08-23 three-view wiring, P6).

    ``anyone`` is the default and the historical behaviour. ``roles``
    admits any caller whose role is listed; ``users`` admits the named
    usernames. Admins are ALWAYS admitted regardless of kind: an admin
    can edit or delete the policy anyway, so pretending to lock them
    out is theater, and a users-routed decision whose named user is
    gone must never wedge a parked session forever.
    """

    kind: Literal["anyone", "roles", "users"] = "anyone"
    roles: list[str] = Field(
        default_factory=list,
        description="Admitted roles when kind='roles'.",
    )
    users: list[str] = Field(
        default_factory=list,
        description="Admitted usernames when kind='users'.",
    )

    def allows(self, *, username: str, role: str) -> bool:
        if role == "admin":
            return True
        if self.kind == "anyone":
            return True
        if self.kind == "roles":
            return role in self.roles
        return username in self.users


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
    approvers: ApproverSpec | None = Field(
        default=None,
        description=(
            "Who may decide calls this policy gates. None means anyone. "
            "A policy/llm evaluation may override this per call by "
            "returning an 'approvers' object in its verdict."
        ),
    )


ApprovalDecision = Literal["approved", "rejected", "timeout", "cancelled"]


class ToolApprovalRecord(Identifiable):
    """One durable, resolved tool-approval decision.

    Written exactly once at the moment an approval gate is finalized
    (operator approved/rejected, or a yield timeout/cancel synthesised a
    decision) by every resume path: agent sessions, graph sessions, and
    chats. The Approvals records view reads these back to show resolved
    history alongside the live (still-parked) pending calls.

    Fields are captured from the parked ``resume_metadata`` blob being
    resolved: ``original_call`` carries the gated ``(id, name, arguments)``
    and ``policy_id`` / ``approval_type`` / ``gate_reason`` come from the
    gate that tripped.
    """

    _id_prefix: ClassVar[str] = "tool-approval-record"

    toolset_id: str | None = Field(
        default=None,
        description="Toolset id of the gated tool, when known.",
    )
    tool_name: str = Field(
        ...,
        description="Bare name of the gated tool.",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments the gated call was invoked with.",
    )
    tool_call_id: str | None = Field(
        default=None,
        description="Id of the gated tool call this decision resolves.",
    )
    agent_id: str | None = Field(
        default=None,
        description="Agent that issued the gated call, when known.",
    )
    session_id: str | None = Field(
        default=None,
        description="Session the gate parked on (None for chat-surface gates).",
    )
    chat_id: str | None = Field(
        default=None,
        description="Chat the gate parked on (None for session/graph gates).",
    )
    requested_at: datetime | None = Field(
        default=None,
        description="When the gate first parked awaiting a decision.",
    )
    decided_at: datetime = Field(
        ...,
        description="When the decision was finalized.",
    )
    decision: ApprovalDecision = Field(
        ...,
        description="Resolved verdict.",
    )
    reason: str | None = Field(
        default=None,
        description="Operator reason or the canned timeout/cancel reason.",
    )
    policy_id: str | None = Field(
        default=None,
        description="Approval policy that gated the call, when one applied.",
    )
    approval_type: str | None = Field(
        default=None,
        description="Gate strategy that tripped (required|policy|llm).",
    )
    gate_reason: str | None = Field(
        default=None,
        description="Why the gate tripped, as surfaced to the operator.",
    )
    principal: str | None = Field(
        default=None,
        description="Gating principal (caller identity), when available.",
    )
    decided_by: str | None = Field(
        default=None,
        description=(
            "Username that made the decision. None for synthesized "
            "verdicts (timeout/cancel) and pre-P6 records."
        ),
    )


__all__ = [
    "ApprovalConfig",
    "ApprovalDecision",
    "ApprovalType",
    "ApproverSpec",
    "LlmApprovalConfig",
    "PolicyApprovalConfig",
    "RequiredApprovalConfig",
    "ToolApprovalPolicy",
    "ToolApprovalRecord",
]
