"""M4: a policy is compiled once per version; every evaluation after that only sets the input and queries."""

from __future__ import annotations

import pytest

regopy = pytest.importorskip("regopy")

from primer.agent.rego import RegoCompileError, RegoEvaluator, evaluate_policy  # noqa: E402

POLICY = """
package primer.tool_approval
import rego.v1
default required := false
required if input.tool_name == "shell_exec"
reason := "shell" if required
"""
OTHER = POLICY.replace("shell_exec", "rm")


def _count_add_module(monkeypatch) -> list[int]:
    count = [0]
    real = regopy.Interpreter

    class Counting(real):  # type: ignore[misc, valid-type]
        def add_module(self, *a, **kw):
            count[0] += 1
            return super().add_module(*a, **kw)

    monkeypatch.setattr(regopy, "Interpreter", Counting)
    return count


def test_verdicts_match_the_one_shot_evaluation_across_changing_inputs() -> None:
    evaluator = RegoEvaluator()
    for tool in ("shell_exec", "ls", "shell_exec", "other", "ls"):
        inp = {"tool_name": tool}
        assert evaluator.evaluate(policy_id="p", policy_text=POLICY, input=inp) == evaluate_policy(POLICY, inp)


def test_the_module_is_added_once_per_policy_version(monkeypatch) -> None:
    count = _count_add_module(monkeypatch)
    evaluator = RegoEvaluator()
    for i in range(20):
        evaluator.evaluate(policy_id="p", policy_text=POLICY, input={"tool_name": f"t{i}"})
    assert count[0] == 1


def test_an_edited_policy_takes_effect_and_compiles_once_more(monkeypatch) -> None:
    count = _count_add_module(monkeypatch)
    evaluator = RegoEvaluator()
    assert evaluator.evaluate(policy_id="p", policy_text=POLICY, input={"tool_name": "rm"}).required is False
    assert evaluator.evaluate(policy_id="p", policy_text=OTHER, input={"tool_name": "rm"}).required is True
    assert evaluator.evaluate(policy_id="p", policy_text=OTHER, input={"tool_name": "rm"}).required is True
    assert count[0] == 2
    assert len(evaluator._interpreters) == 1  # the superseded version is dropped


def test_a_bad_policy_raises_every_time_and_is_not_cached() -> None:
    evaluator = RegoEvaluator()
    for _ in range(2):
        with pytest.raises(RegoCompileError):
            evaluator.evaluate(policy_id="bad", policy_text="package primer.tool_approval\nthis is not rego {{", input={})
    assert not evaluator._interpreters
