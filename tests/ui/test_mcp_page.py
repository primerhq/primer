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


def test_extract_error_reads_extensions_not_detail() -> None:
    """R5 fix (same bug class as ADM_extractError/AT_extractError):
    envelope.detail is always a STRING (primer/api/errors.py's
    _http_exception_handler), the {code, message} dict this used to
    check `typeof ... === "object"` against lives under
    envelope.extensions - code was always null."""
    src = _src()
    start = src.index("function MC_extractError(")
    end = src.index("\n}", start)
    body = src[start:end]
    assert "env.extensions" in body
    assert "env.detail" not in body


def test_search_filter_present() -> None:
    """notes section 4: MCP allowlist wants a search box over the catalogue."""
    src = _src()
    assert "mcp-tool-search" in src
    assert "setSearch" in src


def test_allowed_only_filter_shows_live_count() -> None:
    """notes section 4: "allowed · N" - the Allowed-only filter surfaces
    the live count, not just a bare checkbox."""
    src = _src()
    assert "mcp-allowed-only-filter" in src
    assert "draft.size" in src


def test_pager_present() -> None:
    """notes section 4: the allowlist table wants a pager. Client-side
    (the catalogue endpoint has no server offset/limit and select-all /
    draft / search all need the full array in memory anyway), built
    over the shared Pager component rather than hand-rolled controls."""
    src = _src()
    assert "window.primerApi" in src
    assert "Pager" in src
    assert "pager={pager}" in src


def test_checkboxes_gated_on_master_toggle() -> None:
    """notes section 4: "Checkboxes disabled while the master toggle is
    off" - both the per-row checkbox and the toggle handler itself must
    refuse while the endpoint is disabled, not just the input's visual
    disabled state (a stale/reused draft edit could otherwise still
    flip a value with the input hidden behind a race)."""
    src = _src()
    assert "masterEnabled" in src
    start = src.index("const toggleScoped = ")
    end = src.index("\n  };", start)
    body = src[start:end]
    assert "if (!masterEnabled) return;" in body
