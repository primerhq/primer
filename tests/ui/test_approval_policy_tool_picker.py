"""Approval policy form: Tool row uses the shared y/w/r/n tool picker
(uiv2 Wave 3, approved judgment call: ToolPicker's new single-select
mode, added specifically for this consumer).

RETARGETED: this used to pin a bespoke AP_ToolPicker search browser
that sat ALONGSIDE a free-text toolset-select + tool-name-input pair -
three controls for two fields. The mockup specs one compact field
("Tool: workspace__write_file (the shared toolset__tool picker)"), so
Wave 3 deleted AP_ToolPicker and the free-text pair entirely and wired
window.ToolPicker (the same component agents.jsx uses, in mode="single")
directly to toolsetId/toolName.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
APPROVALS = UI / "components" / "approvals.jsx"
TOOL_PICKER = UI / "components" / "shared" / "tool-picker.jsx"


def _src() -> str:
    return APPROVALS.read_text(encoding="utf-8")


def _picker_src() -> str:
    return TOOL_PICKER.read_text(encoding="utf-8")


def test_bespoke_picker_and_free_text_pair_are_gone() -> None:
    src = _src()
    assert "function AP_ToolPicker(" not in src
    assert "AP_INTERNAL_TOOLSETS" not in src
    assert 'data-testid="approval-policy-toolset"' not in src
    assert 'data-testid="approval-policy-tool"' not in src


def test_modal_mounts_the_shared_picker_in_single_mode() -> None:
    src = _src()
    assert "<window.ToolPicker" in src
    assert 'mode="single"' in src


def test_picking_a_tool_splits_the_scoped_id_into_toolset_and_name() -> None:
    src = _src()
    assert "setToolsetId(sep < 0" in src
    assert "setToolName(sep < 0" in src


def test_shared_picker_supports_single_select_mode() -> None:
    # The mode itself lives in tool-picker.jsx, not approvals.jsx -
    # confirm the component that got the new capability actually has it.
    src = _picker_src()
    assert 'mode === "single"' in src
    assert 'type={single ? "radio" : "checkbox"}' in src


def test_single_mode_hides_bulk_select_and_selected_filter() -> None:
    # Neither a toolset "select all" nor a "selected · N" filter make
    # sense when at most one tool can ever be picked.
    src = _picker_src()
    assert "{!single && (" in src


def test_multi_select_default_is_unchanged() -> None:
    # No `mode` prop = the exact behavior every existing multi-select
    # consumer (agents.jsx) already depends on.
    src = _picker_src()
    assert "const single = mode ===" in src
    assert "single ? new Set([scopedId]) : TP_toggleSet(selected, scopedId)" in src


def test_bundle_transpiles_with_the_shared_picker_wired_in() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    _etag, body = build_jsx_bundle(UI)
    decoded = body.decode("utf-8")
    assert "window.ToolPicker" in decoded
    assert "AP_NewPolicyModal" in decoded
