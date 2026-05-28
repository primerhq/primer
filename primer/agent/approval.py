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
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from primer.agent.rego import RegoCompileError, RegoEvaluator
from primer.int.storage import Storage
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.model.tool_approval import (
    ApprovalType,
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
    """Result of a gate evaluation."""

    required: bool
    reason: str | None = None


class ApprovalResolver:
    """Per-app-instance lookup + cache for ToolApprovalPolicy rows.

    Lookup key is ``(toolset_id, tool_name)``. The application-level
    uniqueness constraint guarantees at most one match. Entries are
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
                predicate, OffsetPage(offset=0, length=1),
            )
            policy = page.items[0] if page.items else None
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
        return ApprovalVerdict(required=True, reason=None)

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
            required=verdict.required, reason=verdict.reason,
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
        return ApprovalVerdict(required=required, reason=reason)

    return ApprovalVerdict(
        required=True,
        reason=f"unknown approval type {cfg.type!r}; gating conservatively",
    )


__all__ = [
    "ApprovalContext",
    "ApprovalResolver",
    "ApprovalVerdict",
    "evaluate_approval_gate",
]
