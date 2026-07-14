"""Agent form shows compaction-prompt help text."""

from __future__ import annotations

from pathlib import Path


JSX = Path(__file__).resolve().parents[2] / "ui" / "components" / "agents.jsx"


def test_help_text_present() -> None:
    src = JSX.read_text(encoding="utf-8")
    assert "Leave blank to use the default prompt" in src
    assert "preserve system context" in src


def test_compaction_tool_access_toggle_present() -> None:
    src = JSX.read_text(encoding="utf-8")
    # State + request body are wired.
    assert "compactionToolAccess" in src
    assert "setCompactionToolAccess" in src
    assert "compaction_tool_access: compactionToolAccess" in src
    # Rendered as a sliding toggle switch (role="switch"), not a checkbox.
    assert "function AG_Toggle(" in src
    assert 'role="switch"' in src
    assert 'testid="na-compaction-tool-access"' in src
    assert "Tool access during compaction" in src


def test_agents_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    ui_dir = JSX.resolve().parents[1]
    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(ui_dir)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    assert "/* === components/agents.jsx === */" in body.decode("utf-8")
