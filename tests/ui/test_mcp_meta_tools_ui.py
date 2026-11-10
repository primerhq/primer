"""Tools Universal M2 console contract for MCP exposure modes."""

from __future__ import annotations

from pathlib import Path


SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "mcp.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_console_exposes_the_two_persisted_modes_accessibly() -> None:
    src = _src()
    assert 'data-testid="mcp-exposure-mode"' in src
    assert 'role="radiogroup"' in src
    assert 'aria-label="MCP exposure mode"' in src
    assert 'value="rbac"' in src
    assert 'value="allowlist"' in src
    assert "RBAC floor" in src
    assert "Allowlist narrowing" in src


def test_mode_update_does_not_send_or_clear_the_saved_allowlist() -> None:
    src = _src()
    start = src.index("const changeMode = ")
    end = src.index("\n  };", start)
    body = src[start:end]
    assert '{ mode: nextMode }' in body
    request = next(line for line in body.splitlines() if 'apiFetch("PUT"' in line)
    assert "allowed_tools" not in request


def test_allowlist_editor_is_preserved_but_inactive_in_rbac_mode() -> None:
    src = _src()
    assert "allowlistActive" in src
    assert "Saved allowlist is preserved" in src
    assert "if (!allowlistActive) return;" in src


def test_mutations_keep_focus_and_report_success_with_toasts() -> None:
    src = _src()
    assert "toastPush" in src
    assert 'busy={busy}' in src
    assert 'busy={saving}' in src
    assert "MCP exposure mode saved" in src
    assert "MCP allowlist saved" in src


def test_catalogue_has_explicit_empty_and_mobile_overflow_states() -> None:
    src = _src()
    assert "No exposable tools are available." in src
    assert 'data-testid="mcp-tools-scroll"' in src
    assert 'overflowX: "auto"' in src
    assert 'minWidth: 760' in src


def test_visible_exposable_memo_body_matches_its_deps() -> None:
    """The memo selects on the same gate its deps track (allowlistActive)."""
    src = _src()
    start = src.index("const visibleExposable = React.useMemo(")
    end = src.index(");", start) + 2
    memo = src[start:end]
    assert "allowlistActive ? pageItems.filter((it) => it.exposable)" in memo
    assert "masterEnabled" not in memo
    assert "[pageItems, allowlistActive]" in memo


def test_rbac_mode_tooltips_name_the_inactive_allowlist_editor() -> None:
    """RBAC-mode disabled checkboxes explain the mode, not a missing catalogue."""
    src = _src()
    assert src.count('"Allowlist editor is inactive in RBAC-floor mode"') == 2
