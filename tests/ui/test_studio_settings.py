"""Structural-presence checks for the Studio → Workspace Settings overlay.

The Studio (studio.jsx) replaced the old WorkspaceDetail page and only
carried over its files / sessions / activity tabs. The remaining tabs —
channels (reply-binding), config, git-log, and destroy — were orphaned.
studio-settings.jsx restores them behind a gear button in the slim sub-header
by RE-USING the exact WorkspaceDetail panel components (WS_ChannelsTab /
WS_ConfigTab / WS_LogTab / WS_DestroyTab), which workspaces.jsx now exports on
window.*.

Like the rest of tests/ui these are static-source + bundle-build checks; they
do not render React.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
STUDIO = UI / "components" / "studio.jsx"
SETTINGS = UI / "components" / "studio-settings.jsx"
WORKSPACES = UI / "components" / "workspaces.jsx"
INDEX = UI / "index.html"


def _settings_src() -> str:
    return SETTINGS.read_text(encoding="utf-8")


def _studio_src() -> str:
    return STUDIO.read_text(encoding="utf-8")


def _workspaces_src() -> str:
    return WORKSPACES.read_text(encoding="utf-8")


def _index_order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_settings_button_in_slim_header() -> None:
    src = _studio_src()
    # A gear button lives in the sub-header next to the workspace selector.
    assert 'data-testid="studio-settings-btn"' in src
    assert 'name="settings"' in src
    # It toggles the overlay open.
    assert "setSettingsOpen(true)" in src


def test_studio_renders_workspace_settings_overlay() -> None:
    src = _studio_src()
    # The overlay is rendered from the header via the window.* export (load-order
    # independent) and is wired the workspace id + pushToast.
    assert "window.WorkspaceSettings" in src
    assert "wid={wid}" in src
    assert "pushToast={pushToast}" in src


def test_settings_surface_and_sections_present() -> None:
    src = _settings_src()
    # Stable testid for the surface + the four restored sections.
    assert 'data-testid="workspace-settings"' in src
    for sec in ("channels", "config", "log", "destroy"):
        assert '{ id: "' + sec + '"' in src, sec


def test_settings_reuses_workspace_detail_panels() -> None:
    src = _settings_src()
    # The four orphaned features are the SAME WorkspaceDetail panels, not
    # reimplementations — resolved from window.* at render time.
    assert "window.WS_ChannelsTab" in src
    assert "window.WS_ConfigTab" in src
    assert "window.WS_LogTab" in src
    assert "window.WS_DestroyTab" in src
    # The reused components are actually rendered (JSX element tags).
    assert "<ChannelsTab" in src
    assert "<ConfigTab" in src
    assert "<LogTab" in src
    assert "<DestroyTab" in src
    # Panels get the resources they expect, keyed like WorkspaceDetail so the
    # useResource cache is shared.
    assert '"workspace-detail:" + wid' in src
    assert '"workspace-sessions:" + wid' in src
    assert "sessionsForBadge={sessionsForBadge}" in src


def test_workspaces_exports_reused_panels() -> None:
    src = _workspaces_src()
    # workspaces.jsx must surface the panels on window.* AND keep WorkspaceDetail
    # (its original renderer) intact.
    assert "window.WS_ChannelsTab = WS_ChannelsTab;" in src
    assert "window.WS_ConfigTab = WS_ConfigTab;" in src
    assert "window.WS_LogTab = WS_LogTab;" in src
    assert "window.WS_DestroyTab = WS_DestroyTab;" in src
    assert "window.WorkspaceDetail = WorkspaceDetail;" in src


def test_reused_panels_preserve_labels() -> None:
    """The e2e locators depend on the panels' visible labels/roles; reusing the
    components preserves them. Assert the load-bearing strings still live in the
    (untouched) panel source in workspaces.jsx."""
    src = _workspaces_src()
    # Destroy: the primary button label + the panel component.
    assert "Destroy workspace" in src
    assert "function WS_DestroyTab(" in src
    # Channels / reply binding.
    assert "function WS_ChannelsTab(" in src
    assert ">Reply binding<" in src
    # Config + log panels.
    assert "function WS_ConfigTab(" in src
    assert "function WS_LogTab(" in src


def test_settings_registered_in_index_after_workspaces() -> None:
    order = _index_order()
    assert "components/studio-settings.jsx" in order
    # Loaded after workspaces.jsx (which defines the panels it re-exports) and
    # before app.jsx.
    assert order.index("components/workspaces.jsx") < order.index("components/studio-settings.jsx")
    assert order.index("components/studio-settings.jsx") < order.index("app.jsx")


def test_bundle_transpiles_with_settings() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/studio-settings.jsx === */" in text
    assert "/* === components/workspaces.jsx === */" in text
    assert "/* === components/studio.jsx === */" in text
