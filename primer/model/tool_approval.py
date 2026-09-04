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

    01a068da: written at RESPOND-TIME (the moment the operator's decision
    actually arrives - ``tool_approval.py``'s ``_publish_decision``, the
    single-park agent-session respond path) for approved/rejected
    verdicts, with a resume-time write as a FALLBACK for verdicts that
    never go through respond at all (a synthesised timeout/cancel) and as
    crash recovery (a process that dies between the respond-time write
    attempt and its confirmation still gets the record written when the
    session is eventually resumed). ``gate_event_key`` carries a UNIQUE
    index (see ``primer.storage.postgres._HOT_FIELD_INDEXES`` /
    ``primer.storage.sqlite._SQLITE_HOT_FIELD_INDEXES``, NULL-tolerant so
    pre-migration rows are unaffected) so the two write sites cannot both
    land a row for the SAME gate: whichever writes second hits the unique
    constraint and is treated as an expected no-op, not a failure. Before
    this fix the ONLY write happened at resume-time, best-effort, so a
    crash between respond and resume lost the audit record entirely - see
    the git history on this docstring for the prior, less accurate,
    claim ("written exactly once... by every resume path").

    This durability upgrade covers the single-park agent-session respond
    path specifically (``/sessions/{id}/tool_approval/respond``). The
    graph engine's multi-concurrent-node approval resolution
    (``primer.worker.graph_resume_coordinator.write_approval_record_for_
    graph``, matched by ``tool_call_id`` rather than a single
    session-level ``event_key``) is a distinct mechanism this change does
    NOT extend to a respond-time write - it keeps its existing
    resume-time-only, best-effort behaviour and its own analogous gap.

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
    gate_event_key: str | None = Field(
        default=None,
        description=(
            "The park's own event_key (ParkedState.yielded.event_key) - "
            "None for records written before this field existed. Carries "
            "a NULL-tolerant unique index, so a record is queryable by "
            "gate and the respond-time + resume-time write sites cannot "
            "both land a row for the same gate."
        ),
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
