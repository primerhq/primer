"""Structural-presence checks for the Studio left sidebar (PR-B / B2).

Asserts:
  - studio-sidebar.jsx exists and exports the expected symbols via window.*
  - sessionStatus helper covers every spec-§7 tone/badge combination
  - StudioSidebar wires into studio.jsx (replaces the region-sidebar placeholder)
  - index.html loads studio-sidebar.jsx before studio.jsx
  - The whole bundle (incl. studio-sidebar.jsx) transpiles cleanly
  - data-testids specified in the port-map are present in the source
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SIDEBAR = UI / "components" / "studio-sidebar.jsx"
STUDIO = UI / "components" / "studio.jsx"
INDEX = UI / "index.html"


def _sidebar_src() -> str:
    return SIDEBAR.read_text(encoding="utf-8")


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


def test_sidebar_file_exists() -> None:
    assert SIDEBAR.exists(), "studio-sidebar.jsx must exist"


def test_sidebar_exports_all_required_symbols() -> None:
    src = _sidebar_src()
    for sym in (
        "window.StudioSidebar = StudioSidebar",
        "window.SessionsSection = SessionsSection",
        "window.FilesTree = FilesTree",
        "window.ST_sessionStatus = ST_sessionStatus",
    ):
        assert sym in src, f"Missing export: {sym}"


# ---------------------------------------------------------------------------
# sessionStatus logic (spec §7 mappings)
# ---------------------------------------------------------------------------


def test_session_status_running() -> None:
    src = _sidebar_src()
    # running + not parked → green-pulse
    assert "green-pulse" in src
    assert '"running"' in src


def test_session_status_paused() -> None:
    src = _sidebar_src()
    assert "paused" in src
    assert '"amber"' in src


def test_session_status_approval_badge() -> None:
    src = _sidebar_src()
    assert '"approve"' in src


def test_session_status_ask_user_badge() -> None:
    src = _sidebar_src()
    assert '"ask"' in src


def test_session_status_watch_files_badge() -> None:
    src = _sidebar_src()
    assert '"watch"' in src


def test_session_status_sleep_badge() -> None:
    src = _sidebar_src()
    assert '"sleep"' in src


def test_session_status_ended_gray() -> None:
    src = _sidebar_src()
    assert '"gray"' in src
    # ended / completed / cancelled → gray
    assert '"ended"' in src or "ended" in src


def test_session_status_failed_red() -> None:
    src = _sidebar_src()
    assert '"red"' in src
    assert '"failed"' in src


def test_session_status_created_dim() -> None:
    src = _sidebar_src()
    assert '"dim"' in src
    assert '"created"' in src


# ---------------------------------------------------------------------------
# data-testids (port-map §4.3)
# ---------------------------------------------------------------------------


def test_sidebar_testids() -> None:
    src = _sidebar_src()
    required = [
        'data-testid="sessions-header"',
        'data-testid="session-row"',
        'data-testid="session-status-dot"',
        'data-testid="files-header"',
        'data-testid="file-row"',
        'data-testid="new-session-btn"',
        'data-testid="hidden-toggle"',
        # new-session-form / new-session-name moved into the shared component
        # (ui/components/new-session-form.jsx) when the two create forms were
        # unified (FD2); they're asserted in test_shared_new_session_form.py.
    ]
    for tid in required:
        assert tid in src, f"Missing data-testid: {tid}"


# ---------------------------------------------------------------------------
# Studio shell wires in StudioSidebar (no more placeholder in the left region)
# ---------------------------------------------------------------------------


def test_studio_sidebar_wired_into_studio_shell() -> None:
    src = _studio_src()
    # Placeholder must be gone from the left region.
    assert 'testid="region-sidebar"' not in src, (
        "ST_RegionPlaceholder for region-sidebar must be replaced by <StudioSidebar>"
    )
    # The real component is rendered.
    assert "<StudioSidebar wid={wid}" in src


# ---------------------------------------------------------------------------
# index.html load order: studio-sidebar.jsx before studio.jsx
# ---------------------------------------------------------------------------


def test_index_loads_sidebar_before_studio() -> None:
    order = _index_order()
    assert "components/studio-sidebar.jsx" in order, "studio-sidebar.jsx not in index.html"
    assert "components/studio.jsx" in order, "studio.jsx not in index.html"
    assert order.index("components/studio-sidebar.jsx") < order.index("components/studio.jsx"), (
        "studio-sidebar.jsx must be loaded before studio.jsx"
    )


def test_index_loads_shared_before_sidebar() -> None:
    order = _index_order()
    assert order.index("components/shared.jsx") < order.index("components/studio-sidebar.jsx")


# ---------------------------------------------------------------------------
# Bundle transpile (the hard gate: JSX must compile cleanly)
# ---------------------------------------------------------------------------


def test_bundle_transpiles_with_studio_sidebar() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/studio-sidebar.jsx === */" in text
    assert "/* === components/studio.jsx === */" in text


# ---------------------------------------------------------------------------
# Graph-session glyph + delete + rename + create-name (bugs #9, #20, #22)
# ---------------------------------------------------------------------------


def test_sidebar_exports_session_kind_helpers() -> None:
    src = _sidebar_src()
    assert "window.ST_sessionKind = ST_sessionKind" in src
    assert "window.ST_sessionGlyph = ST_sessionGlyph" in src


def test_session_kind_detects_graph_prefix() -> None:
    # Graph-bound sessions carry a synthetic agent_id "graph:<gid>" on the
    # SessionInfo list shape (no binding field), so the prefix is the signal.
    src = _sidebar_src()
    assert 'indexOf("graph:") === 0' in src


def test_sidebar_session_management_testids() -> None:
    src = _sidebar_src()
    for tid in (
        'data-testid="session-delete"',
        'data-testid="session-rename"',
    ):
        assert tid in src, f"Missing data-testid: {tid}"


def test_sidebar_delete_uses_modal_not_native_confirm() -> None:
    src = _sidebar_src()
    # The delete confirm must go through the shared Modal, never window.confirm.
    assert "ST_SessionDeleteDialog" in src
    assert "window.confirm" not in src


def test_sidebar_delete_stops_row_open_propagation() -> None:
    src = _sidebar_src()
    # The trash button must stopPropagation so the row's open-on-click
    # doesn't fire when deleting.
    assert "e.stopPropagation(); setPendingDelete(session)" in src


def test_sidebar_rename_patches_session() -> None:
    src = _sidebar_src()
    assert "ST_SessionRenameDialog" in src
    assert '"PATCH"' in src
