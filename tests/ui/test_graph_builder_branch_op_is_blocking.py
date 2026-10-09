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
