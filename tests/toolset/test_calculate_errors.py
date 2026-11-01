"""``misc.calculate`` answers every expression it cannot evaluate as a tool error (AG-L4).

Wrong arity (``min()``, ``abs()``, ``max(1)``, ``pow(2, 0.5, 3)``) raised ``TypeError`` past the handler, and an integer result over Python's 4300-digit
string limit raised ``ValueError`` from the JSON encoding after the power had already burnt the CPU on the event loop (``11**10**7``). A power whose result
would be over that limit is now refused before it is computed. The cases are the review probe (``/tmp/review-agent/toolset/test_calculate.py``) inverted.
"""

from __future__ import annotations

import json

import pytest

from primer.toolset.misc import build_misc_toolset


async def _calc(expression: str):
    return await build_misc_toolset().call(tool_name="calculate", arguments={"expression": expression})


@pytest.mark.parametrize(
    "expression",
    ["min()", "abs()", "max(1)", "round(1.5, 2, 3)", "pow(2, 0.5, 3)", "10**5000", "11**10**7", "7**7**7", "pow(7, 7**7)", "10**4000 * 10**4000"],
)
async def test_an_expression_that_cannot_be_evaluated_is_a_bad_request_answer(expression: str) -> None:
    result = await _calc(expression)

    assert result.is_error is True
    assert json.loads(result.output)["type"] == "bad-request"


async def test_a_power_too_large_to_answer_is_refused_naming_the_limit() -> None:
    result = await _calc("11**10**7")

    assert "4300" in json.loads(result.output)["message"]


# ---- characterization: answers and successes that did not change --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "value"),
    [
        ("2 + 2", 4),
        ("2**10", 1024),
        ("pow(2, 10)", 1024),
        ("pow(2, 10, 1000)", 24),
        ("2**-1", 0.5),
        ("(-2)**3", -8),
        ("10**4299", 10**4299),
        ("9**4500 // 9**4499", 9),
        ("2.0**10", 1024.0),
        ("min(3, 1, 2)", 1),
        ("round(2.567, 2)", 2.57),
        ("sqrt(16) * 2", 8.0),
        ("1**100000000", 1),
        ("0**100000000", 0),
        ("(-1)**100000001", -1),
    ],
)
async def test_characterization_results_are_unchanged(expression: str, value) -> None:
    result = await _calc(expression)

    assert result.is_error is False
    assert json.loads(result.output) == {"expression": expression, "result": value}


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("2 +", "syntax error in expression: invalid syntax"),
        ("1 / 0", "evaluation error: ZeroDivisionError: division by zero"),
        ("2.0**10000", "evaluation error: OverflowError: (34, 'Numerical result out of range')"),
        ("sqrt(-1)", "math domain error"),
        ("foo(1)", "unknown function 'foo'; allowed: ['abs', 'acos', 'asin', 'atan', 'ceil', 'cos', 'exp', 'floor', 'log', 'log10', 'log2', 'max', 'min', 'pow', 'round', 'sin', 'sqrt', 'tan']"),
        ("x", "unknown name 'x'; allowed constants: ['e', 'pi', 'tau']"),
        ("'a'", "unsupported literal: 'a' (only int/float)"),
    ],
)
async def test_characterization_existing_error_answers_are_unchanged(expression: str, message: str) -> None:
    result = await _calc(expression)

    assert result.is_error is True
    assert json.loads(result.output) == {"type": "bad-request", "message": message}
