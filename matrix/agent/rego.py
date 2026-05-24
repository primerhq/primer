"""Thin wrapper around regopy for the tool-approval gate.

Exposes :func:`evaluate_policy` for one-shot evaluation and
:class:`RegoEvaluator` for the long-lived in-process cache used by
:class:`matrix.agent.approval.ApprovalResolver`.

The cache is keyed by ``(policy_id, hash(policy_text))`` so a policy
edit invalidates its own entry automatically without an explicit flush.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from matrix.model.except_ import ProviderError


logger = logging.getLogger(__name__)


class RegoCompileError(ProviderError):
    """Raised when a Rego policy cannot be parsed/compiled."""


@dataclass
class RegoVerdict:
    """Result of one policy evaluation against an input document."""

    required: bool
    reason: str | None = None


_PACKAGE_QUERY = "data.matrix.tool_approval"


def _coerce_verdict(raw: Any) -> RegoVerdict:
    """Project a regopy Output (or its parsed dict) into a :class:`RegoVerdict`.

    The regopy ``Output.__str__`` renders JSON like::

        {"expressions":[{"required":true, "reason":"..."}]}

    We parse that string and extract the first expression value.
    """
    # raw may be an Output object (has __str__) or already a dict
    if not isinstance(raw, dict):
        try:
            raw_str = str(raw)
            parsed = json.loads(raw_str)
        except (ValueError, TypeError) as exc:
            raise RegoCompileError(
                f"could not parse regopy output as JSON: {exc}"
            ) from exc
    else:
        parsed = raw

    # Unwrap {"expressions": [...]} wrapper produced by regopy
    if isinstance(parsed, dict) and "expressions" in parsed:
        expressions = parsed["expressions"]
        if not isinstance(expressions, list) or not expressions:
            raise RegoCompileError(
                "policy produced an empty result set; the package "
                "must define a `required` rule"
            )
        doc = expressions[0]
    else:
        doc = parsed

    if not isinstance(doc, dict):
        raise RegoCompileError(
            f"policy result must be an object, got {type(doc).__name__}"
        )

    required = doc.get("required")
    if not isinstance(required, bool):
        raise RegoCompileError(
            "policy result missing required boolean `required` field"
        )

    reason_raw = doc.get("reason")
    reason: str | None
    if reason_raw is None or (isinstance(reason_raw, str) and reason_raw == ""):
        reason = None
    elif isinstance(reason_raw, str):
        reason = reason_raw
    else:
        reason = str(reason_raw)

    return RegoVerdict(required=required, reason=reason)


def evaluate_policy(policy_text: str, input: dict[str, Any]) -> RegoVerdict:
    """One-shot evaluation (no cache). Useful for the create-time validator."""
    try:
        import regopy
    except ImportError as exc:  # pragma: no cover — declared in pyproject
        raise RegoCompileError(
            "regopy is required for tool-approval policies"
        ) from exc
    try:
        interpreter = regopy.Interpreter()
        interpreter.add_module("matrix_tool_approval", policy_text)
        interpreter.set_input(input)
        raw = interpreter.query(_PACKAGE_QUERY)
        if not raw.ok():
            raise RegoCompileError(f"rego eval failed: {raw}")
    except RegoCompileError:
        raise
    except Exception as exc:
        raise RegoCompileError(f"rego compile/eval failed: {exc}") from exc
    return _coerce_verdict(raw)


class RegoEvaluator:
    """Long-lived evaluator with a content-addressed cache.

    Each entry is keyed by ``(policy_id, sha256(policy_text))`` so an
    edit to the policy invalidates only that entry. Cache size is
    bounded by the number of distinct policies the operator has
    written; no eviction policy beyond letting the dict grow.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], str] = {}
        self.cache_hits = 0

    def evaluate(
        self,
        *,
        policy_id: str,
        policy_text: str,
        input: dict[str, Any],
    ) -> RegoVerdict:
        digest = hashlib.sha256(policy_text.encode("utf-8")).hexdigest()
        key = (policy_id, digest)
        if key in self._cache:
            self.cache_hits += 1
        else:
            self._cache[key] = policy_text
        return evaluate_policy(policy_text, input)


__all__ = [
    "RegoCompileError",
    "RegoEvaluator",
    "RegoVerdict",
    "evaluate_policy",
]
