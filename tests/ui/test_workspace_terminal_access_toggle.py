"""Workspace instance overlay: Terminal - user access toggle (notes 3.8).

Real bug fixed backend-side in batch 1: the field never existed on
WorkspaceRow at all, so terminal.py's gate always fell through to False
regardless of operator intent. This pins that the console actually calls
the real PUT /workspaces/{id}/terminal_access endpoint and reflects the
real WorkspaceRow.terminal_user_access field, not a static "always on"
stub (component-inventory finding #8 in the prototype).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
WORKSPACES = UI / "components" / "workspaces.jsx"


def _src() -> str:
    return WORKSPACES.read_text(encoding="utf-8")


def test_toggle_component_exists() -> None:
    src = _src()
    assert "function WS_TerminalAccessToggle(" in src


def test_reads_the_real_field() -> None:
    src = _src()
    assert "data.terminal_user_access" in src


def test_writes_through_the_real_endpoint() -> None:
    src = _src()
    assert "/terminal_access" in src
    assert '"PUT"' in src


def test_toggle_is_not_a_static_stub() -> None:
    # The prototype's version hardcoded the "on" background regardless of
    # any prop or field (finding #8) -- this one derives from `enabled`,
    # which comes from the fetched row, and calls the API on click.
    src = _src()
    start = src.index("function WS_TerminalAccessToggle(")
    end = src.index("\nfunction WS_ConfigTab(")
    body = src[start:end]
    assert "const enabled = !!data.terminal_user_access;" in body
    assert "onClick={toggle}" in body
    assert 'data-testid="workspace-terminal-access-toggle"' in body


def test_wired_into_the_config_tab() -> None:
    src = _src()
    assert "<WS_TerminalAccessToggle wid={wid} ws={ws} />" in src


def test_errors_surface_inline() -> None:
    src = _src()
    assert 'data-testid="workspace-terminal-access-error"' in src


def test_bundle_transpiles_with_the_toggle() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    _etag, body = build_jsx_bundle(UI)
    assert "WS_TerminalAccessToggle" in body.decode("utf-8")
