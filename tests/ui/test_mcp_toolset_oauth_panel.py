"""MCP toolset overlay OAuth section (R4 Workbench group, notes 3.9).

McpToolsetProvider.complete_oauth() and the callback route
(GET/POST /toolsets/{id}/oauth/callback, batch-2 item 3) both exist, but
the production toolset factory never builds a PrimerOAuthHandler for a
toolset, so complete_oauth always 503s "OAuth not configured for this
provider" today. This pins that the console renders that TRUE state
rather than a fake success -- no config, no button; a configured toolset
gets a real button that calls the real route and shows whatever it says.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
TOOLSETS = UI / "components" / "toolsets.jsx"


def _src() -> str:
    return TOOLSETS.read_text(encoding="utf-8")


def test_panel_exists() -> None:
    src = _src()
    assert "function TS_McpOAuthPanel(" in src


def test_no_oauth_config_shows_the_honest_unconfigured_note() -> None:
    src = _src()
    # Exact copy of the backend's ConfigError message (McpToolsetProvider.
    # complete_oauth), so the two surfaces never say something different.
    assert "OAuth not configured for this provider." in src
    assert 'data-testid="toolset-oauth-unconfigured"' in src


def test_relink_button_calls_the_real_callback_route() -> None:
    src = _src()
    assert 'data-testid="toolset-oauth-relink"' in src
    assert "/oauth/callback" in src


def test_relink_result_is_read_from_the_real_response_not_hardcoded() -> None:
    # The success path is a generic "Reconnected." (it would be TRUE if the
    # backend ever answers 200), but the failure path reads err.detail --
    # today's guaranteed content is the backend's own "OAuth not configured
    # for this provider" ConfigError message, never a hardcoded string here.
    src = _src()
    assert 'data-testid="toolset-oauth-result"' in src
    assert "err.detail" in src or "err && (err.detail" in src


def test_panel_only_renders_for_mcp_http_and_sse() -> None:
    src = _src()
    idx = src.index("TS_McpOAuthPanel id={ts.id}")
    guard = src[max(0, idx - 200):idx]
    assert 'ts?.provider === "mcp"' in guard
    assert 'transport === "http"' in guard
    assert 'transport === "sse"' in guard


def test_bundle_transpiles_with_the_oauth_panel() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    _etag, body = build_jsx_bundle(UI)
    assert "TS_McpOAuthPanel" in body.decode("utf-8")
