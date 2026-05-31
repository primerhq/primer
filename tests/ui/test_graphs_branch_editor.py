"""Conditional-edge branch editor wired with operator dropdown + default_to."""
from pathlib import Path
SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "graphs.jsx"
def _src() -> str: return SRC.read_text(encoding="utf-8")

def test_branch_editor_renders_for_conditional_edge() -> None:
    src = _src()
    assert "branches" in src and "default_to" in src
    assert "BranchCondition" in src or "conditions" in src

def test_operator_dropdown_lists_all_ops() -> None:
    src = _src()
    for op in ["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "exists"]:
        assert f'"{op}"' in src, f"missing operator {op}"
