"""Thin wrapper around regopy for the tool-approval gate.

Exposes :func:`evaluate_policy` for one-shot evaluation and
:class:`RegoEvaluator` for the long-lived in-process cache used by
:class:`primer.agent.approval.ApprovalResolver`.

The cache is keyed by ``(policy_id, hash(policy_text))`` so a policy
edit invalidates its own entry automatically without an explicit flush.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any

from primer.model.except_ import ProviderError


logger = logging.getLogger(__name__)


class RegoCompileError(ProviderError):
    """Raised when a Rego policy cannot be parsed/compiled."""


@dataclass
class RegoVerdict:
    """Result of one policy evaluation against an input document."""

    required: bool
    reason: str | None = None
    # Optional per-call approver routing (P6): the raw 'approvers'
    # object from the result document, validated by the approval layer.
    approvers: Any | None = None


_PACKAGE_QUERY = "data.primer.tool_approval"


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

    return RegoVerdict(
        required=required,
        reason=reason,
        approvers=doc.get("approvers"),
    )


def _regopy() -> Any:
    try:
        import regopy
    except (ImportError, OSError) as exc:
        # ImportError = regopy not installed.
        # OSError = regopy installed but its ctypes shared object
        # (`rego` cdll) can't be loaded — typically a missing
        # system dep like libatomic.so.1 on slim Docker images.
        # Either case is an environmental misconfiguration, not a
        # bad policy; surface as a RegoCompileError so the
        # validator returns a clean 422 instead of leaking 500.
        raise RegoCompileError(
            f"regopy unavailable for tool-approval policies: {exc}"
        ) from exc
    return regopy


def _compile(policy_text: str) -> Any:
    """A regopy interpreter holding the policy module (the expensive step)."""
    regopy = _regopy()
    try:
        interpreter = regopy.Interpreter()
        interpreter.add_module("primer_tool_approval", policy_text)
    except Exception as exc:
        raise RegoCompileError(f"rego compile/eval failed: {exc}") from exc
    return interpreter


def _run(interpreter: Any, input: dict[str, Any]) -> RegoVerdict:
    try:
        interpreter.set_input(input)
        raw = interpreter.query(_PACKAGE_QUERY)
        if not raw.ok():
            raise RegoCompileError(f"rego eval failed: {raw}")
    except RegoCompileError:
        raise
    except Exception as exc:
        raise RegoCompileError(f"rego compile/eval failed: {exc}") from exc
    return _coerce_verdict(raw)


def evaluate_policy(policy_text: str, input: dict[str, Any]) -> RegoVerdict:
    """One-shot evaluation (no cache). Useful for the create-time validator."""
    return _run(_compile(policy_text), input)


class RegoEvaluator:
    """Long-lived evaluator: one compiled interpreter per ``(policy_id, sha256(policy_text))``.

    An edit to a policy gets a new key, and the superseded version of that policy id is dropped, so the cache holds one interpreter per policy.
    The interpreter is not re-entrant (``set_input`` then ``query`` is one critical section), so evaluations take one lock; they are short and
    CPU-bound, and compiling is the cost this saves.
    """

    def __init__(self) -> None:
        self._interpreters: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()
        self.cache_hits = 0

    def evaluate(
        self,
        *,
        policy_id: str,
        policy_text: str,
        input: dict[str, Any],
    ) -> RegoVerdict:
        key = (policy_id, hashlib.sha256(policy_text.encode("utf-8")).hexdigest())
        with self._lock:
            interpreter = self._interpreters.get(key)
            if interpreter is None:
                interpreter = _compile(policy_text)  # raises for a bad policy before anything is cached
                for stale in [k for k in self._interpreters if k[0] == policy_id]:
                    del self._interpreters[stale]
                self._interpreters[key] = interpreter
            else:
                self.cache_hits += 1
            return _run(interpreter, input)


__all__ = [
    "RegoCompileError",
    "RegoEvaluator",
    "RegoVerdict",
    "evaluate_policy",
]
