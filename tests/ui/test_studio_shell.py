"""Structural-presence checks for the Studio shell (PR-B / B1 foundation).

The Studio is the workspace-scoped IDE view that replaces the thin
workspace-detail page at /workspaces/:wid. B1 lays down the route shell,
the useStudioState model, and the persistence + URL-mirroring seams; the
left/center/right regions are styled placeholders that B2-B4 fill.

These tests assert the seams exist and the bundle still transpiles. They
do NOT render React (the ui/ suite is static-source + bundle-build only,
matching test_session_frame_extracted.py).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
STUDIO = UI / "components" / "studio.jsx"
INDEX = UI / "index.html"
STYLES = UI / "styles.css"
APP = UI / "app.jsx"


def _studio_src() -> str:
    return STUDIO.read_text(encoding="utf-8")


def _index_order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_studio_file_exists_and_exports() -> None:
    src = _studio_src()
    assert "function Studio(" in src
    assert "function StudioHeader(" in src
    assert "function useStudioState(" in src
    # No-build window exports so app.jsx + B2-B4 can reach them.
    assert "window.Studio = Studio;" in src
    assert "window.StudioHeader = StudioHeader;" in src
    assert "window.useStudioState = useStudioState;" in src


def test_studio_shell_root_and_header_testids() -> None:
    src = _studio_src()
    assert 'data-testid="studio-root"' in src
    assert 'data-testid="studio-header"' in src
    assert 'data-testid="workspace-selector"' in src


def test_studio_region_placeholders_present() -> None:
    src = _studio_src()
    # All three body-region wrapper divs must still carry their stable testids
    # (the wrappers are kept even after a region is filled).
    for testid in ("studio-sidebar", "studio-center", "studio-activity"):
        assert f'data-testid="{testid}"' in src, testid
    # B2 (left sidebar) is now filled — StudioSidebar replaced the placeholder.
    assert "<StudioSidebar wid={wid}" in src, "B2 StudioSidebar not wired in"
    assert 'testid="region-sidebar"' not in src, "B2 placeholder should be gone"
    # B3 (center) is now filled — StudioCenter replaced the placeholder.
    assert "<StudioCenter wid={wid}" in src, "B3 StudioCenter not wired in"
    assert 'testid="region-center"' not in src, "B3 placeholder should be gone"
    # B4 (right sidebar) is now filled — StudioActivity replaced the placeholder.
    assert "<StudioActivity wid={wid}" in src, "B4 StudioActivity not wired in"
    assert 'testid="region-activity"' not in src, "B4 placeholder should be gone"


def test_use_studio_state_persistence_contract() -> None:
    src = _studio_src()
    # localStorage key + the exact persistence + URL seams from
    # STUDIO-INTEGRATION.md §3.
    assert '"studio:" + wid' in src
    assert "ST_loadPersisted" in src
    assert "ST_savePersisted" in src
    assert "ST_tabFromUrl" in src
    assert "ST_syncUrl" in src
    assert "history.replaceState" in src
    # Active-tab URL mirror uses the ?open=session:/?open=file: contract.
    assert "open" in src
    # The tab model + sidebar toggles B2-B4 consume.
    for action in ("openTab", "focusTab", "closeTab", "toggleSessions", "toggleFiles", "toggleHidden"):
        assert action in src, action


def test_deep_link_synthesizes_url_tab() -> None:
    src = _studio_src()
    # A fresh deep-link (#/workspaces/:wid?open=session:<sid>) with empty
    # localStorage must still create + activate the tab, not mount empty.
    # The synthesis helper turns the parsed ?open= id into a minimal tab.
    assert "function ST_tabFromUrlId(" in src
    # session:<id> → a session tab; file:<path> → a file tab.
    assert 'kind: "session"' in src
    assert 'kind: "file"' in src
    # The helper is wired through ST_applyUrlTab, which both the lazy
    # initializer and the wid-change effect call, so a missing url tab is
    # appended (concat) and activated rather than ignored.
    assert "ST_applyUrlTab" in src
    assert "ST_tabFromUrlId(urlTab)" in src
    # When the url tab isn't already open we append + activate it.
    assert "base.openTabs = openTabs.concat([tab]);" in src
    assert "base.activeTabId = urlTab;" in src


def test_workspace_selector_uses_workspaces_resource() -> None:
    src = _studio_src()
    # Selector lists GET /v1/workspaces and navigates on pick.
    assert "/workspaces?limit=200" in src
    assert "useResource" in src


def test_studio_registered_in_index_after_workspace_tap() -> None:
    order = _index_order()
    assert "components/studio.jsx" in order
    # studio.jsx reuses Icon (shared.jsx) + WorkspaceTap is its B4 neighbour.
    assert order.index("components/shared.jsx") < order.index("components/studio.jsx")
    assert order.index("components/studio.jsx") < order.index("app.jsx")


def test_app_renders_studio_for_workspace_detail() -> None:
    src = APP.read_text(encoding="utf-8")
    # /workspaces/:wid now renders the Studio shell directly.
    # B5 added pushToast prop; check the Studio render is present with wid.
    assert "<Studio wid={currentWorkspaceId}" in src
    # /sessions and /sessions/:id redirect into the Studio.
    assert '"#/workspaces"' in src
    assert "open=session:" in src
    assert "workspace_id" in src


def test_styles_has_studio_tokens_and_classes() -> None:
    css = STYLES.read_text(encoding="utf-8")
    assert "--teal:" in css
    assert "--teal-dim:" in css
    assert "--frow-h:" in css
    for cls in (".st-root", ".st-body", ".st-topbar", ".st-section", ".st-tabbar"):
        assert cls in css, cls


def test_bundle_transpiles_with_studio() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/studio.jsx === */" in text
