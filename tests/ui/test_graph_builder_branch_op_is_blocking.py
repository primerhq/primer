"""A branch condition op outside the model's Literal must be a BLOCKING GB_validate issue.

The model (``primer/model/graph.py``, ``BranchCondition.op``) is
``Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "exists"]``. An op such as
``contains`` passed the import shape check (text-only) and GB_validate today, was drawn, and
then Save failed with a 422 on ``BranchCondition.op`` (ticket 01a1200d item 3, #680 review N).
The builder's own op list is ``GB_OPS`` in ``ui/components/graph-builder/gb-branches.jsx``;
the validator must refuse, at Save-blocking tier, any op outside that list, naming the step
and the branch in words like the other blocking messages.

The harness mirrors ``test_graph_builder_parity._validate`` (same GB_validate call in
MiniRacer) but also loads ``gb-branches.jsx`` so ``window.GB_OPS`` is defined, exactly as it
is in the real builder surface where all graph-builder files share one window.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GB = ROOT / "ui" / "components" / "graph-builder"


def _src(name: str) -> str:
    return (GB / name).read_text(encoding="utf-8")


def _builder_ops() -> list[str]:
    """The GB_OPS list exactly as the builder ships it in gb-branches.jsx."""
    m = re.search(r"const GB_OPS = \[([^\]]*)\];", _src("gb-branches.jsx"))
    assert m, "GB_OPS moved out of gb-branches.jsx: update this pin"
    return re.findall(r'"([^"]+)"', m.group(1))


def _ops_decl() -> str:
    """The GB_OPS const declaration, as plain JS the MiniRacer context can eval (the file itself is JSX)."""
    m = re.search(r"const GB_OPS = \[[^\]]*\];", _src("gb-branches.jsx"))
    assert m, "GB_OPS moved out of gb-branches.jsx: update this pin"
    return m.group(0)


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis; window.primerApi = {};")
    for name in ("gb-model.jsx", "gb-refs.jsx", "gb-validate.jsx"):
        ctx.eval(_src(name))
    # A bare const eval binds the global lexical scope, not the window object
    # the validator reads, so hand it to window as the real surface does.
    ctx.eval(_ops_decl() + " window.GB_OPS = GB_OPS;")
    return ctx


def _validate(draft: dict) -> dict:
    return json.loads(_ctx().eval(f"JSON.stringify(GB_validate({json.dumps(draft)}, {{}}))"))


def _draft(op: str) -> dict:
    """begin -> agent "Pick" -> (json_path branch on op) -> end."""
    return {
        "nodes": [
            {"kind": "begin", "id": "s"},
            {"kind": "agent", "id": "a", "agent_id": "x", "description": "Pick"},
            {"kind": "end", "id": "e", "output_template": ""},
        ],
        "edges": [
            {"kind": "static", "from_node": "s", "to_node": "a"},
            {
                "kind": "conditional",
                "from_node": "a",
                "router": {
                    "kind": "json_path",
                    "branches": [
                        {"conditions": [{"path": "k", "op": op, "value": 1}], "to_node": "e"},
                        {"conditions": [], "to_node": "e"},
                    ],
                    "default_to": "e",
                },
            },
        ],
    }


def test_the_builder_op_list_matches_the_model_literal() -> None:
    from typing import get_args

    from primer.model.graph import BranchCondition

    assert _builder_ops() == list(get_args(BranchCondition.model_fields["op"].annotation))


def test_an_op_outside_the_literal_is_a_blocking_issue() -> None:
    found = _validate(_draft("contains"))
    rows = [r for r in found["blocking"] if r["code"] == "branch_op_unknown"]
    assert rows, f"op 'contains' must be blocking, got {json.dumps(found)[:400]}"
    message = rows[0]["message"]
    assert "Pick" in message, f"the message must name the step in words: {message}"
    assert "branch 1" in message, f"the message must name the branch in words: {message}"


@pytest.mark.parametrize("op", _builder_ops())
def test_every_op_the_model_allows_is_not_flagged(op: str) -> None:
    found = _validate(_draft(op))
    codes = [r["code"] for r in found["blocking"]]
    assert "branch_op_unknown" not in codes, f"op {op!r} must not be flagged: {json.dumps(found)[:400]}"


def test_a_branch_with_no_target_is_a_blocking_issue() -> None:
    """'Add a path' creates to_node: "" and the PUT 422s on it (JsonPathBranch.to_node min_length=1)."""
    draft = _draft("eq")
    draft["edges"][1]["router"]["branches"][0]["to_node"] = ""
    found = _validate(draft)
    rows = [r for r in found["blocking"] if r["code"] == "branch_no_target"]
    assert rows, f"an empty to_node must be blocking, got {json.dumps(found)[:400]}"
    message = rows[0]["message"]
    assert "Pick" in message, f"the message must name the step in words: {message}"
    assert "branch 1" in message, f"the message must name the branch in words: {message}"


def test_a_branch_with_a_real_target_is_not_flagged() -> None:
    codes = [r["code"] for r in _validate(_draft("eq"))["blocking"]]
    assert "branch_no_target" not in codes, json.dumps(codes)


def test_a_bad_op_on_the_second_branch_names_the_second_branch() -> None:
    """Pin: the message counts the branch, it is not hard-coded to the first one."""
    draft = _draft("eq")
    draft["edges"][1]["router"]["branches"][1]["conditions"] = [{"path": "k", "op": "contains", "value": 1}]
    found = _validate(draft)
    rows = [r for r in found["blocking"] if r["code"] == "branch_op_unknown"]
    assert rows, f"the bad op on branch 2 must be blocking, got {json.dumps(found)[:400]}"
    message = rows[0]["message"]
    assert "Pick" in message, f"the message must name the step in words: {message}"
    assert "branch 2" in message, f"the message must name the second branch: {message}"


def test_an_empty_target_on_the_second_branch_names_the_second_branch() -> None:
    """Pin: the empty-target message counts the branch, it is not hard-coded to the first one."""
    draft = _draft("eq")
    draft["edges"][1]["router"]["branches"][1]["to_node"] = ""
    found = _validate(draft)
    rows = [r for r in found["blocking"] if r["code"] == "branch_no_target"]
    assert rows, f"an empty to_node on branch 2 must be blocking, got {json.dumps(found)[:400]}"
    message = rows[0]["message"]
    assert "Pick" in message, f"the message must name the step in words: {message}"
    assert "branch 2" in message, f"the message must name the second branch: {message}"


def _branch_rows_fn() -> str:
    """The branchRowsFor arrow from gb-outline.jsx, as plain JS (the block itself carries no JSX)."""
    m = re.search(r"const branchRowsFor = \(nodeId\) => \{.*?\n  \};", _src("gb-outline.jsx"), re.S)
    assert m, "branchRowsFor moved out of gb-outline.jsx or its shape changed: update this pin"
    return m.group(0)


def _outline_rows(draft: dict, node_id: str) -> list[dict]:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval(f"var draft = {json.dumps(draft)};")
    ctx.eval("var byId = {}; for (var n of draft.nodes || []) byId[n.id] = n;")
    ctx.eval(_branch_rows_fn())
    return json.loads(ctx.eval(f"JSON.stringify(branchRowsFor({json.dumps(node_id)}))"))


def test_a_router_with_no_kind_is_checked_as_json_path() -> None:
    """The model defaults the router kind to "json_path" (_JsonPathRouter.kind) and a pasted import can omit it; a kind-less router must still get the json_path checks (branch_op_unknown, branch_requires_response_format, no_catch_all)."""
    draft = _draft("eq")
    draft["edges"][1]["router"]["branches"][0]["conditions"][0]["op"] = "contains"
    del draft["edges"][1]["router"]["kind"]
    found = _validate(draft)
    rows = [r for r in found["blocking"] if r["code"] == "branch_op_unknown"]
    assert rows, f"a kind-less router must be checked as json_path, got {json.dumps(found)[:400]}"


def test_a_router_with_no_kind_draws_its_paths_in_the_outline() -> None:
    """Same default on the left rail: a kind-less json_path router must draw its branch rows like one that names the kind."""
    bare = _draft("eq")
    del bare["edges"][1]["router"]["kind"]
    named = _draft("eq")
    rows_bare = _outline_rows(bare, "a")
    rows_named = _outline_rows(named, "a")
    assert rows_named, "the named-kind control drew nothing: the harness is not reaching branchRowsFor"
    assert rows_bare == rows_named, f"the kind-less router draws {json.dumps(rows_bare)}, the json_path one draws {json.dumps(rows_named)}"
