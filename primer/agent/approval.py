"""Approval gate: resolver + context + verdict + evaluator.

The :class:`ApprovalResolver` is the cached lookup interface
threaded into :class:`primer.agent.tool_manager.ToolExecutionManager`.
:func:`evaluate_approval_gate` dispatches by ``ApprovalType`` to the
required / Rego / LLM judge backends.

All failure modes inside the gate fail CLOSED — an unhealthy judge
or a broken Rego policy produces ``ApprovalVerdict(required=True,
reason=<diagnostic>)`` so a sensitive call never slips through
silently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from primer.agent.rego import RegoCompileError, RegoEvaluator
from primer.int.storage import Storage
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.model.tool_approval import (
    ApprovalType,
    ApproverSpec,
    LlmApprovalConfig,
    PolicyApprovalConfig,
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)


if TYPE_CHECKING:
    from primer.api.registries.provider_registry import ProviderRegistry


logger = logging.getLogger(__name__)


@dataclass
class ApprovalContext:
    """Per-dispatch context handed to every approval evaluator."""

    tool_name: str
    toolset_id: str
    arguments: dict[str, Any]
    agent_id: str | None
    session_id: str | None
    chat_id: str | None
    requested_at: datetime  # tz-aware UTC

    def to_input_doc(self) -> dict[str, Any]:
        """Shape sent to Rego / LLM judge."""
        return {
            "tool_name": self.tool_name,
            "toolset_id": self.toolset_id,
            "arguments": self.arguments,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "requested_at": self.requested_at.isoformat(),
        }


@dataclass
class ApprovalVerdict:
    """Result of a gate evaluation.

    ``approvers`` is the per-call routing a policy/llm evaluation may
    return (P6); None falls back to the policy row's own ``approvers``,
    and None there means anyone.
    """

    required: bool
    reason: str | None = None
    approvers: ApproverSpec | None = None


def _coerce_approvers(raw: Any, *, policy_id: str) -> ApproverSpec | None:
    """Validate an evaluator-returned approvers object, fail-soft.

    Routing is advisory metadata on top of an already-tripped gate: a
    malformed spec must not fail the call OR silently widen access, so
    it degrades to None (= the policy row's own spec, or anyone).
    """
    if raw is None:
        return None
    try:
        return ApproverSpec.model_validate(raw)
    except Exception:  # noqa: BLE001
        logger.warning(
            "policy %s returned a malformed approvers object %r; "
            "falling back to the policy-level spec", policy_id, raw,
        )
        return None


def effective_approvers(
    policy: ToolApprovalPolicy, verdict: ApprovalVerdict,
) -> ApproverSpec | None:
    """Per-call routing wins over the policy row's default."""
    return verdict.approvers or policy.approvers


# How many enabled candidates for one (toolset_id, tool_name) the resolver reads. One is the rule; more than one is a leftover
# duplicate, and a handful is already pathological, so this is a bound on the read and not a limit anyone should meet.
_CANDIDATES = 50


class _DuplicateGate(RequiredApprovalConfig):
    """The unconditional gate that stands in for several policies of one tool; ``reason`` is shown on the approval card and the record."""

    reason: str


def _choose_policy(rows: Sequence[ToolApprovalPolicy], *, toolset_id: str, tool_name: str) -> ToolApprovalPolicy | None:
    """The policy that gates ``(toolset_id, tool_name)`` among its ENABLED rows, by the set and never by the order they came in.

    Uniqueness per tool is refused at write time (REST and the system tools share ``check_policy_unique``) but is not a storage
    constraint, so a raced pair of creates or a row from before the check can leave two. The old pick, the first row of an unordered
    ``find``, depended on the backend's row order: a weaker duplicate could shadow a strict one and the winner could change with no edit.

    The rule is MOST RESTRICTIVE WINS, on both axes a policy controls (newest cannot be told: a policy carries no timestamp):

    * one row: that row, as stored;
    * WHETHER the call is gated: an unconditional (``required``) policy among them wins, and when all are conditional (Rego, LLM
      judge) their strictness depends on the call, which is not known here, so the tool is gated unconditionally, failing closed as
      the evaluator does for an error. The kinds are only required, policy and llm, so "a gate is required" is never weaker;
    * WHO may decide it: a conditional verdict can route the call to specific approvers per call, which a replaced gate loses, and
      rows may carry different ``approvers``. Unless every candidate is unconditional with one identical approver spec, ONLY AN ADMIN
      may decide (``ApproverSpec`` always admits an admin whatever its kind, so this is never wider than any single duplicate; it can
      be narrower than all of them, which is the safe side, until the extra row is deleted).

    The chosen copy keeps the identity and timeout of the lowest-id candidate (of the unconditional ones if there are any), a stored
    row, so the approval record and the card still name a real policy; its gate reason names the duplicates. The ids are also logged,
    at WARNING, on each cache miss (at most once per ``cache_ttl_seconds`` per key per process, and again after ``invalidate``).
    """
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    ordered = sorted(rows, key=lambda p: (p.approval.type is not ApprovalType.REQUIRED, p.id))
    first = ordered[0]
    ids = [p.id for p in ordered]
    all_unconditional = all(p.approval.type is ApprovalType.REQUIRED for p in ordered)
    admin_only = not all_unconditional or any(p.approvers != first.approvers for p in ordered)
    reason = f"duplicate approval policies for this tool ({', '.join(ids)}); delete the extra rows"
    if admin_only:
        reason += "; until then only an admin may decide it"
    chosen = first.model_copy(update={
        "approval": _DuplicateGate(reason=reason),
        "approvers": ApproverSpec(kind="roles", roles=[]) if admin_only else first.approvers,
    })
    logger.warning(
        "tool approval: %d enabled policies for toolset_id=%r tool_name=%r (%s); gated unconditionally under the settings of %r%s%s. "
        "Delete the extra rows.",
        len(rows), toolset_id, tool_name, ", ".join(ids), first.id,
        ", decided by an admin only" if admin_only else "",
        "; more rows may exist than were read" if len(rows) >= _CANDIDATES else "",
    )
    return chosen


def approval_resume_metadata(
    *,
    policy: ToolApprovalPolicy,
    verdict: ApprovalVerdict,
    original_call: dict[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    """The ``resume_metadata`` of an approval park, built in ONE place for EVERY site that parks for approval.

    Carries the identity of the gate (``policy_id``, ``approval_type``, ``gate_reason``), the call it gates (``original_call``) and WHO MAY
    DECIDE it: the effective approver spec (the evaluator's per-call routing, else the policy row's), stamped as a dict or ``None`` for
    "anyone". Every path that answers a gate judges the answer against this stamp (:func:`primer.session.approvers.may_decide`), so a
    park site that built its own dict and left the stamp out (the ``call_tool`` meta-dispatch did) silently let any user decide a
    restricted gate. ``extra`` carries a site's own keys (``via_call_tool``).
    """
    approvers = effective_approvers(policy, verdict)
    return {
        "policy_id": policy.id,
        "approval_type": policy.approval.type.value,
        "gate_reason": verdict.reason,
        "approvers": approvers.model_dump() if approvers is not None else None,
        **extra,
        "original_call": original_call,
    }


class ApprovalResolver:
    """Per-app-instance lookup + cache for ToolApprovalPolicy rows.

    Lookup key is ``(toolset_id, tool_name)``. Uniqueness is refused at
    write time, not by storage, so more than one enabled row can exist
    (see :func:`_choose_policy` for what the lookup then does). Entries are
    cached in-process for ``cache_ttl_seconds`` (default 30 s) so
    operator edits propagate without a restart.
    """

    def __init__(
        self,
        storage: Storage[ToolApprovalPolicy],
        *,
        cache_ttl_seconds: float = 30.0,
    ) -> None:
        self._storage = storage
        self._ttl = cache_ttl_seconds
        # Cache value layout: (expires_at_monotonic, policy_or_None)
        self._cache: dict[tuple[str, str], tuple[float, ToolApprovalPolicy | None]] = {}
        self._lock = asyncio.Lock()

    async def find(
        self,
        *,
        toolset_id: str,
        tool_name: str,
    ) -> ToolApprovalPolicy | None:
        key = (toolset_id, tool_name)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]
            # Build a compound predicate:
            #   (toolset_id == X AND tool_name == Y) AND enabled == True
            #
            # The outer shape lets callers (including test fakes) navigate:
            #   predicate.left.left.right.value  -> toolset_id value
            #   predicate.left.right.right.value -> tool_name value
            toolset_pred = Predicate(
                left=FieldRef(name="toolset_id"),
                op=Op.EQ,
                right=Value(value=toolset_id),
            )
            tool_pred = Predicate(
                left=FieldRef(name="tool_name"),
                op=Op.EQ,
                right=Value(value=tool_name),
            )
            key_pred = Predicate(
                left=toolset_pred,
                op=Op.AND,
                right=tool_pred,
            )
            enabled_pred = Predicate(
                left=FieldRef(name="enabled"),
                op=Op.EQ,
                right=Value(value=True),
            )
            predicate = Predicate(
                left=key_pred,
                op=Op.AND,
                right=enabled_pred,
            )
            page = await self._storage.find(
                predicate, OffsetPage(offset=0, length=_CANDIDATES),
            )
            policy = _choose_policy(page.items, toolset_id=toolset_id, tool_name=tool_name)
            self._cache[key] = (now + self._ttl, policy)
            return policy

    def invalidate(self) -> None:
        """Drop the in-process cache; next lookup hits storage."""
        self._cache.clear()


async def _dispatch_llm_judge(
    *,
    policy: LlmApprovalConfig,
    context: ApprovalContext,
    provider_registry: "ProviderRegistry",
) -> dict[str, Any]:
    """Invoke the configured LLM judge.

    Sends a chat-completion request with the policy's system prompt
    plus a user message carrying the pretty-printed approval context.
    Asks for structured output matching the verdict schema; returns
    the parsed dict.
    """
    llm = await provider_registry.get_llm(policy.provider_id)
    schema = {
        "type": "object",
        "properties": {
            "required": {"type": "boolean"},
            "reason": {"type": "string"},
            # Optional per-call routing (P6): who may decide this call.
            "approvers": {
                "type": "object",
                "properties": {
                    "kind": {"enum": ["anyone", "roles", "users"]},
                    "roles": {"type": "array", "items": {"type": "string"}},
                    "users": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["kind"],
                "additionalProperties": False,
            },
        },
        "required": ["required"],
        "additionalProperties": False,
    }
    raw = await llm.judge_structured(
        model=policy.model,
        system_prompt=policy.prompt,
        user_message=json.dumps(context.to_input_doc(), indent=2),
        response_schema=schema,
    )
    if not isinstance(raw, dict) or "required" not in raw:
        raise RuntimeError(
            f"llm judge returned malformed verdict: {raw!r}"
        )
    return raw


_rego_evaluator = RegoEvaluator()


async def evaluate_approval_gate(
    *,
    policy: ToolApprovalPolicy,
    context: ApprovalContext,
    provider_registry: "ProviderRegistry | None",
) -> ApprovalVerdict:
    """Dispatch by approval type. Always fail closed on errors."""
    cfg = policy.approval
    if cfg.type == ApprovalType.REQUIRED:
        assert isinstance(cfg, RequiredApprovalConfig)
        # Only the duplicate fallback (``_DuplicateGate``) carries a reason; a stored required policy has none.
        return ApprovalVerdict(required=True, reason=getattr(cfg, "reason", None))

    if cfg.type == ApprovalType.POLICY:
        assert isinstance(cfg, PolicyApprovalConfig)
        try:
            verdict = _rego_evaluator.evaluate(
                policy_id=policy.id,
                policy_text=cfg.policy,
                input=context.to_input_doc(),
            )
        except RegoCompileError as exc:
            logger.warning(
                "rego policy %s failed; failing closed: %s", policy.id, exc,
            )
            return ApprovalVerdict(
                required=True,
                reason="policy evaluation failed; gating conservatively",
            )
        except Exception as exc:
            logger.exception("unexpected rego failure on policy %s", policy.id)
            return ApprovalVerdict(
                required=True,
                reason=f"rego gate errored: {exc}",
            )
        return ApprovalVerdict(
            required=verdict.required,
            reason=verdict.reason,
            approvers=_coerce_approvers(
                verdict.approvers, policy_id=policy.id,
            ),
        )

    if cfg.type == ApprovalType.LLM:
        assert isinstance(cfg, LlmApprovalConfig)
        if provider_registry is None:
            return ApprovalVerdict(
                required=True,
                reason="llm gate unavailable: no provider registry",
            )
        try:
            raw = await _dispatch_llm_judge(
                policy=cfg, context=context,
                provider_registry=provider_registry,
            )
        except Exception as exc:
            logger.warning(
                "llm judge for policy %s failed; failing closed: %s",
                policy.id, exc,
            )
            return ApprovalVerdict(
                required=True,
                reason="llm gate unavailable; gating conservatively",
            )
        required = bool(raw.get("required"))
        reason_raw = raw.get("reason")
        reason = (
            reason_raw if isinstance(reason_raw, str) and reason_raw else None
        )
        return ApprovalVerdict(
            required=required,
            reason=reason,
            approvers=_coerce_approvers(
                raw.get("approvers"), policy_id=policy.id,
            ),
        )

    return ApprovalVerdict(
        required=True,
        reason=f"unknown approval type {cfg.type!r}; gating conservatively",
    )


__all__ = [
    "ApprovalContext",
    "ApprovalResolver",
    "ApprovalVerdict",
    "approval_resume_metadata",
    "effective_approvers",
    "evaluate_approval_gate",
]
