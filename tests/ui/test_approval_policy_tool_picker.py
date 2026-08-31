"""Approval policy form: searchable tool catalogue picker with y/w/r/n
badges (R4 Governance group, notes 3.4/3.11's "one picker everywhere").

Before this the policy form's only way to name the gated tool was two
free-text inputs (a toolset select + a raw tool-name text box) with no
way to see the tool's capabilities. This adds a searchable browser over
GET /tools (the same endpoint TS_ToolsTab reads, batch-2 + its follow-up
both carry the 4 badge fields there) that fills those same fields on
pick, without removing them.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
APPROVALS = UI / "components" / "approvals.jsx"


def _src() -> str:
    return APPROVALS.read_text(encoding="utf-8")


def test_picker_component_exists() -> None:
    src = _src()
    assert "function AP_ToolPicker(" in src


def test_picker_reads_the_real_tools_endpoint() -> None:
    src = _src()
    assert '"GET", "/tools"' in src


def test_picker_renders_capability_badges_per_row() -> None:
    src = _src()
    assert "<CapabilityBadges tool={r.tool}" in src


def test_picking_a_row_fills_toolset_and_tool_name() -> None:
    src = _src()
    assert "onPick(r.toolsetId, r.tool.id)" in src
    assert "setToolsetId(pickedToolsetId)" in src
    assert "setToolName(pickedToolName)" in src


def test_free_text_fields_are_not_removed() -> None:
    # The picker is additive -- typing a not-yet-indexed toolset/tool id
    # must still work.
    src = _src()
    assert 'data-testid="approval-policy-toolset"' in src
    assert 'data-testid="approval-policy-tool"' in src


def test_unavailable_toolsets_are_skipped() -> None:
    src = _src()
    start = src.index("function AP_ToolPicker(")
    end = src.index("\nfunction AP_NewPolicyModal(")
    body = src[start:end]
    assert "if (!g.available) continue;" in body


def test_bundle_transpiles_with_the_picker() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    _etag, body = build_jsx_bundle(UI)
    assert "AP_ToolPicker" in body.decode("utf-8")
