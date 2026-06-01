"""Static JSX checks for the MCP console page — Spec §11.

These tests are structural: they don't render the page (no jsdom in
the python toolchain), they just assert the surface the e2e suite +
manual smoke depend on:

* The page component is defined and exported on ``window``.
* The two ``data-testid`` anchors the e2e selectors will use exist.
* The PUT body wires up the ``allowed_tools`` field against the
  ``/v1/mcp_exposure`` endpoint.
* The Claude Desktop config copy button is present.
* The "recommend safe defaults" affordance is present.

The runtime behaviour (toggle, save, filter) is exercised by the
operator-driven manual smoke + Phase 7's e2e SDK test of the server
side; this file's job is to pin down the contract so the file can't
be silently renamed / gutted.
"""

from __future__ import annotations

from pathlib import Path


MCP = Path(__file__).resolve().parents[2] / "ui" / "components" / "mcp.jsx"


def _src() -> str:
    return MCP.read_text()


def test_page_component_defined() -> None:
    """A McpPage / MCPPage component must exist for the route handler."""
    src = _src()
    assert "McpPage" in src or "MCPPage" in src


def test_endpoint_panel_testid() -> None:
    """E2E selector for Panel 1 (the endpoint controls)."""
    assert "mcp-endpoint-panel" in _src()


def test_tools_table_testid() -> None:
    """E2E selector for Panel 2 (the exposed-tools table)."""
    assert "mcp-tools-table" in _src()


def test_puts_allowed_tools() -> None:
    """Save flow hits the right endpoint with the right body shape."""
    src = _src()
    assert "/v1/mcp_exposure" in src or "/mcp_exposure" in src
    assert "allowed_tools" in src


def test_claude_desktop_config_present() -> None:
    """The Claude Desktop snippet copies the canonical ``mcpServers`` shape."""
    src = _src()
    assert "mcpServers" in src


def test_save_btn_testid() -> None:
    """E2E selector for the Save button."""
    assert "save-allowed-btn" in _src()


def test_recommend_safe_defaults_button() -> None:
    """A "recommend safe defaults" affordance exists for the conservative set."""
    src = _src().lower()
    assert (
        "safe_defaults" in src
        or "recommend" in src
        or "default" in src
    )
