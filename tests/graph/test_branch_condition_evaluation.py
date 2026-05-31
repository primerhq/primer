"""One test per operator + the missing-path-False rule."""

from __future__ import annotations

import pytest

from primer.graph.router import evaluate_branch_condition
from primer.model.graph import BranchCondition


PARSED = {
    "ok": True,
    "score": 87,
    "tag": "blue",
    "items": [10, 20, 30],
    "nested": {"name": "alice"},
    "maybe_null": None,
}


def _ev(path: str, op: str, value=None) -> bool:
    return evaluate_branch_condition(
        PARSED, BranchCondition(path=path, op=op, value=value)
    )


@pytest.mark.parametrize("op,value,expected", [
    ("eq", True, True),
    ("eq", False, False),
    ("ne", False, True),
    ("ne", True, False),
])
def test_eq_ne(op, value, expected) -> None:
    assert _ev("ok", op, value) is expected


@pytest.mark.parametrize("op,value,expected", [
    ("gt", 50, True),
    ("gt", 87, False),
    ("gte", 87, True),
    ("lt", 90, True),
    ("lt", 87, False),
    ("lte", 87, True),
])
def test_numeric_ordering(op, value, expected) -> None:
    assert _ev("score", op, value) is expected


def test_in_and_not_in() -> None:
    assert _ev("tag", "in", ["red", "blue", "green"]) is True
    assert _ev("tag", "in", ["red", "green"]) is False
    assert _ev("tag", "not_in", ["red", "green"]) is True
    assert _ev("tag", "not_in", ["red", "blue", "green"]) is False


def test_exists_for_present_value() -> None:
    assert _ev("score", "exists") is True


def test_exists_returns_false_for_null_value() -> None:
    """Spec §3.1: exists requires a non-None, non-missing value."""
    assert _ev("maybe_null", "exists") is False


def test_exists_returns_false_for_missing_path() -> None:
    assert _ev("not_there", "exists") is False


@pytest.mark.parametrize("op", ["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in"])
def test_missing_path_is_always_false(op) -> None:
    """The cornerstone semantic: a missing path makes every operator
    False — even ne and not_in. Use exists to test presence."""
    value: object = 1 if op in {"gt", "gte", "lt", "lte"} else ["x"] if op in {"in", "not_in"} else "z"
    assert evaluate_branch_condition(
        PARSED, BranchCondition(path="not_there", op=op, value=value)
    ) is False


def test_non_numeric_against_numeric_operator_returns_false() -> None:
    """Spec §3.2: non-numeric on either side makes gt/gte/lt/lte False
    (NOT an error)."""
    assert _ev("tag", "gt", 5) is False


def test_in_with_non_list_value_returns_false() -> None:
    """`value` MUST be a list for in/not_in; otherwise False."""
    assert _ev("tag", "in", "blue_or_green") is False


def test_bracket_index_path() -> None:
    assert _ev("items[1]", "eq", 20) is True
