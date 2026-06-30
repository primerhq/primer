"""Structural-presence checks for the Studio center region (PR-B / B3).

The center region is the tab bar + the active document panel. B3 builds:
  - CenterTabs        — tab bar over studio.state.openTabs
  - SessionAgentPanel — agent transcript (reuses SessionLiveStream)
  - SessionGraphPanel — graph run-view (reuses SD_GraphRunView)
  - FilePanel         — file preview/edit + 412-conflict save flow
  - StudioCenter      — wires CenterTabs + the active panel into the shell

Asserts the seams + data-testids exist, the reused components are referenced
(NOT reimplemented), StudioCenter replaces the region-center placeholder, and
the whole bundle (incl. studio-center.jsx) transpiles cleanly. Mirrors the
static-source + bundle-build style of test_studio_sidebar.py — no React render.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CENTER = UI / "components" / "studio-center.jsx"
STUDIO = UI / "components" / "studio.jsx"
INDEX = UI / "index.html"


def _center_src() -> str:
    return CENTER.read_text(encoding="utf-8")


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


# ---------------------------------------------------------------------------
# File existence + window exports
# ---------------------------------------------------------------------------


def test_center_file_exists() -> None:
    assert CENTER.exists(), "studio-center.jsx must exist"


def test_center_exports_all_required_symbols() -> None:
    src = _center_src()
    for sym in (
        "window.StudioCenter = StudioCenter",
        "window.CenterTabs = CenterTabs",
        "window.SessionAgentPanel = SessionAgentPanel",
        "window.SessionGraphPanel = SessionGraphPanel",
        "window.FilePanel = FilePanel",
    ):
        assert sym in src, f"Missing export: {sym}"


def test_center_defines_components() -> None:
    src = _center_src()
    for fn in (
        "function StudioCenter(",
        "function CenterTabs(",
        "function SessionAgentPanel(",
        "function SessionGraphPanel(",
        "function FilePanel(",
    ):
        assert fn in src, f"Missing component: {fn}"


# ---------------------------------------------------------------------------
# REUSE — the agent transcript + graph run-view are the production components
# from session-detail.jsx, reached as window globals (NOT reimplemented).
# ---------------------------------------------------------------------------


def test_reuses_session_live_stream() -> None:
    src = _center_src()
    assert "window.SessionLiveStream" in src, "must reuse SessionLiveStream, not rebuild it"
    # Passed the props it already expects.
    for prop in ("sid={sid}", "wid={wid}", "session={session}"):
        assert prop in src, f"SessionLiveStream prop missing: {prop}"


def test_reuses_sd_graph_run_view() -> None:
    src = _center_src()
    assert "window.SD_GraphRunView" in src, "must reuse SD_GraphRunView, not rebuild it"
    # gid from the binding, rid = session id.
    assert "gid={gid}" in src
    assert "rid={sid}" in src


def test_reuses_markdown_and_highlighter() -> None:
    src = _center_src()
    assert "window.renderMarkdown" in src, "markdown preview must reuse vendor renderMarkdown"
    assert "highlightPython" in src, "code preview must reuse the vendored highlighter"


def test_does_not_reimplement_reused_components() -> None:
    src = _center_src()
    # We must not define our own copies of the reused components.
    assert "function SessionLiveStream(" not in src
    assert "function SD_GraphRunView(" not in src
    assert "function GR_Canvas(" not in src
    assert "function renderMarkdown(" not in src


# ---------------------------------------------------------------------------
# Session panel routing: fetch the session, branch on binding.kind.
# ---------------------------------------------------------------------------


def test_session_panel_fetches_and_branches_on_binding_kind() -> None:
    src = _center_src()
    assert "/sessions/" in src, "must GET /v1/sessions/{ref}"
    assert "useResource" in src
    # Branch agent vs graph off binding.kind (with the defensive binding_kind alias).
    assert "binding.kind" in src
    assert "graph" in src and "agent" in src


# ---------------------------------------------------------------------------
# File panel: read holds etag, PUT with etag, 412 → conflict banner.
# ---------------------------------------------------------------------------


def test_file_panel_read_and_save_with_etag() -> None:
    src = _center_src()
    assert "files/read?path=" in src, "FilePanel must GET files/read"
    assert "files?path=" in src, "FilePanel must PUT files?path"
    assert "etag=" in src, "save must send the held etag for optimistic concurrency"
    assert 'encoding: "text"' in src or "encoding:\"text\"" in src


def test_file_panel_handles_412_conflict() -> None:
    src = _center_src()
    assert "412" in src, "save must detect the 412 stale-write status"
    # Reload re-reads + clears dirty; Overwrite re-PUTs without the etag.
    assert "reloadConflict" in src
    assert "overwriteConflict" in src


def test_file_panel_preview_branches() -> None:
    src = _center_src()
    # Image preview via files/download; markdown + code + text branches.
    assert "files/download?path=" in src
    assert "markdown" in src
    assert "<img" in src


def test_file_panel_dirty_via_patch() -> None:
    src = _center_src()
    # Dirty state mirrors into openTabs via the patch() escape hatch.
    assert "studio.patch" in src
    assert "dirty" in src


# ---------------------------------------------------------------------------
# data-testids (port-map §4.4)
# ---------------------------------------------------------------------------


def test_center_testids() -> None:
    src = _center_src()
    required = [
        'data-testid="center-tabs"',
        'data-testid="center-tab"',
        'data-testid="center-tab-close"',
        'data-testid="panel-agent"',
        'data-testid="panel-graph"',
        'data-testid="panel-file"',
        'data-testid="file-mode-preview"',
        'data-testid="file-mode-edit"',
        'data-testid="file-save"',
        'data-testid="file-editor"',
        'data-testid="file-conflict-banner"',
    ]
    for tid in required:
        assert tid in src, f"Missing data-testid: {tid}"


# ---------------------------------------------------------------------------
# Studio shell wires in StudioCenter (no more placeholder in the center region)
# ---------------------------------------------------------------------------


def test_studio_center_wired_into_studio_shell() -> None:
    src = _studio_src()
    assert 'testid="region-center"' not in src, (
        "ST_RegionPlaceholder for region-center must be replaced by <StudioCenter>"
    )
    assert "<StudioCenter wid={wid}" in src


# ---------------------------------------------------------------------------
# index.html load order: studio-center.jsx after session-detail (for the reused
# components) and before studio.jsx.
# ---------------------------------------------------------------------------


def test_index_loads_center_in_order() -> None:
    order = _index_order()
    assert "components/studio-center.jsx" in order, "studio-center.jsx not in index.html"
    # Must load after session-detail.jsx (SessionLiveStream/SD_GraphRunView)
    # and after markdown + shared, before studio.jsx.
    assert order.index("components/session-detail.jsx") < order.index("components/studio-center.jsx")
    assert order.index("components/shared.jsx") < order.index("components/studio-center.jsx")
    assert order.index("vendor/markdown.jsx") < order.index("components/studio-center.jsx")
    assert order.index("components/studio-center.jsx") < order.index("components/studio.jsx")


# ---------------------------------------------------------------------------
# Bundle transpile (the hard gate: the whole bundle incl. studio*.jsx compiles)
# ---------------------------------------------------------------------------


def test_bundle_transpiles_with_studio_center() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/studio-center.jsx === */" in text
    assert "/* === components/studio.jsx === */" in text
    assert "/* === components/studio-sidebar.jsx === */" in text
