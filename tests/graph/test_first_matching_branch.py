"""first_matching_branch routes on JsonPathBranch.conditions
(AND-of-conditions, first-match-wins, empty conditions = catch-all)."""

from __future__ import annotations

from primer.graph.router import first_matching_branch
from primer.model.graph import BranchCondition, JsonPathBranch


def _b(conds, to):
    return JsonPathBranch(conditions=conds, to_node=to)


def test_first_match_wins() -> None:
    branches = [
        _b([BranchCondition(path="ok", op="eq", value=True)], "yes"),
        _b([], "fallback"),
    ]
    m = first_matching_branch({"ok": True}, branches)
    assert m is not None
    assert m.to_node == "yes"


def test_and_of_conditions() -> None:
    branches = [
        _b(
            [
                BranchCondition(path="ok", op="eq", value=True),
                BranchCondition(path="score", op="gt", value=80),
            ],
            "both",
        ),
        _b([], "catchall"),
    ]
    assert first_matching_branch({"ok": True, "score": 90}, branches).to_node == "both"
    assert first_matching_branch({"ok": True, "score": 50}, branches).to_node == "catchall"


def test_empty_conditions_is_catch_all() -> None:
    branches = [_b([], "anything")]
    assert first_matching_branch({}, branches).to_node == "anything"


def test_no_match_returns_none() -> None:
    branches = [
        _b([BranchCondition(path="ok", op="eq", value=True)], "yes"),
    ]
    assert first_matching_branch({"ok": False}, branches) is None
