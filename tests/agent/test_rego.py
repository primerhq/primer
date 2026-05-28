"""Tests for the thin regopy wrapper."""

from __future__ import annotations

import pytest

from primer.agent.rego import RegoCompileError, RegoEvaluator, evaluate_policy


_PASS = """
package primer.tool_approval
default required := false
required if input.tool_name == "shell_exec"
"""

_REASONED = """
package primer.tool_approval
default required := false
default reason := ""
required if input.tool_name == "delete"
reason := "deletion is sensitive" if input.tool_name == "delete"
"""

_INVALID = """
package primer.tool_approval
default required = false  syntax-broken
"""


def test_evaluate_required_true():
    res = evaluate_policy(_PASS, {"tool_name": "shell_exec"})
    assert res.required is True
    assert res.reason is None


def test_evaluate_required_false():
    res = evaluate_policy(_PASS, {"tool_name": "search_documents"})
    assert res.required is False


def test_evaluate_reason_present():
    res = evaluate_policy(_REASONED, {"tool_name": "delete"})
    assert res.required is True
    assert res.reason == "deletion is sensitive"


def test_invalid_policy_raises_compile_error():
    with pytest.raises(RegoCompileError):
        evaluate_policy(_INVALID, {"tool_name": "shell_exec"})


def test_evaluator_caches_compilation():
    ev = RegoEvaluator()
    a = ev.evaluate(policy_id="p1", policy_text=_PASS, input={"tool_name": "x"})
    b = ev.evaluate(policy_id="p1", policy_text=_PASS, input={"tool_name": "x"})
    assert a.required == b.required
    assert ev.cache_hits >= 1
