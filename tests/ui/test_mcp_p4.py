"""Platform wave P4 item 4: the MCP allowlist picker (mcp.jsx's
MC_ToolsPanel) renders the same shared CapabilityBadges the agent tool
picker already does, now that GET /mcp_exposure/available carries the
four y/w/r/n flags per row (primer/mcp/exposure.py's
list_available_tools, platform wave P2 #28).

Static-source checks only (the tests/ui suite convention).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "mcp.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _panel_src() -> str:
    src = _src()
    start = src.index("function MC_ToolsPanel(")
    end = src.index("\n// ====", start)
    return src[start:end]


def test_the_fe_allowlist_picker_exists_and_renders_tool_rows() -> None:
    """Confirms the premise the brief's conditional hinges on - if this
    ever stops being true, the CapabilityBadges wiring below is dead
    code and should be revisited."""
    panel = _panel_src()
    assert 'data-testid={`tool-row-${it.scoped_id}`}' in panel


def test_tool_rows_render_capability_badges() -> None:
    panel = _panel_src()
    assert "<window.primerApi.CapabilityBadges tool={it}" in panel
    assert 'testid={`tool-flags-${it.scoped_id}`}' in panel


def test_badges_column_sits_beside_the_existing_status_verdict_column() -> None:
    """Additive, not a replacement: CapabilityBadges answers "what kind
    of tool is this" (declared yields/workspace/role/notifying), the
    existing Status column answers "can it be exposed at all right
    now" (the is_exposable verdict) - two different questions, matching
    CapabilityBadges' own documented ruling that MCP-effective role
    gating belongs on a verdict column, not the shared badge."""
    panel = _panel_src()
    flags_idx = panel.index("<th style={{ textAlign: \"left\", padding: \"8px 12px\" }}>Flags</th>")
    status_idx = panel.index("<th style={{ textAlign: \"left\", padding: \"8px 12px\" }}>Status</th>")
    assert flags_idx < status_idx


def test_bundle_transpiles_with_mcp_p4() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/mcp.jsx === */" in text
