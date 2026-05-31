"""BranchCondition operator model + JsonPathBranch acceptance of both shapes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.graph import BranchCondition, JsonPathBranch


def test_branch_condition_eq_minimal() -> None:
    c = BranchCondition(path="a.b", op="eq", value=42)
    assert c.path == "a.b"
    assert c.op == "eq"
    assert c.value == 42


def test_branch_condition_exists_no_value() -> None:
    c = BranchCondition(path="x", op="exists")
    assert c.op == "exists"
    assert c.value is None


def test_branch_condition_in_with_list() -> None:
    c = BranchCondition(path="status", op="in", value=["a", "b"])
    assert c.value == ["a", "b"]


def test_branch_condition_invalid_op_rejected() -> None:
    with pytest.raises(ValidationError):
        BranchCondition(path="a", op="like", value="x")


def test_jsonpath_branch_new_shape_default_empty_conditions() -> None:
    b = JsonPathBranch(to_node="n1")
    assert b.conditions == []
    assert b.to_node == "n1"


def test_jsonpath_branch_with_conditions() -> None:
    b = JsonPathBranch(
        conditions=[
            BranchCondition(path="ok", op="eq", value=True),
            BranchCondition(path="count", op="gt", value=5),
        ],
        to_node="next",
    )
    assert len(b.conditions) == 2
