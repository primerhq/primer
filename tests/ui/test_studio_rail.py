"""Studio revamp: one rail, two modes (ui/studio/STUDIO-WIRING.md §5).

The rail's whole value is ordering - "what needs me" becomes a position on
screen instead of a badge. So the tests that matter are about the grouping and
the reuse: that the four groups render in the fixed order, that sorting stays
inside a group, and that the 1000-line FilesTree plus the create form and
dialogs are re-hosted rather than forked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
STUDIO = UI / "components" / "studio.jsx"
RAIL = UI / "components" / "studio" / "st-rail.jsx"
RUNS = UI / "components" / "studio" / "st-runs-rail.jsx"
FILES = UI / "components" / "studio" / "st-files-rail.jsx"
SIDEBAR = UI / "components" / "studio-sidebar.jsx"


def _code_only(src: str) -> str:
    """Strip `//` comment bodies so a prose mention cannot satisfy a test."""
    out = []
    for line in src.splitlines():
        idx = line.find("//")
        out.append(line if idx == -1 else line[:idx])
    return "\n".join(out)


def _ctx():
    """MiniRacer with the pure grouping logic loaded (no React)."""
    py_mini_racer = pytest.importorskip("py_mini_racer")
    ctx = py_mini_racer.MiniRacer()
    ctx.eval((UI / "components" / "studio" / "st-status.jsx").read_text(encoding="utf-8")
             .replace("window.", "var _unused_"))
    # ST_sessionSort is the ordering rule the rail reuses; pull just that
    # function out of the sidebar so its behaviour is exercised for real.
    src = SIDEBAR.read_text(encoding="utf-8")
    start = src.index("function ST_sessionSort(")
    end = src.index("\n}\n", start) + 3
    ctx.eval(src[start:end])
    return ctx


# ---------------------------------------------------------------------------
# Grouping and ordering - the actual behaviour change
# ---------------------------------------------------------------------------


def test_groups_render_in_the_fixed_bucket_order() -> None:
    ctx = _ctx()
    ctx.eval("var order = ST2_BUCKET_ORDER.join(',');")
    assert ctx.eval("order") == "needs,working,broken,done"


def test_sorting_happens_inside_a_group_never_across_it() -> None:
    # A done session that sorts first by ST_sessionSort must still land below
    # every needs-you row, because the group order IS the priority.
    ctx = _ctx()
    ctx.eval("""
        var sessions = [
          {session_id: "a-done",  status: "ended"},
          {session_id: "z-needs", status: "parked", park_reason: "ask_user"},
          {session_id: "m-run",   status: "running"}
        ];
        var groups = {needs: [], working: [], broken: [], done: []};
        sessions.forEach(function (s) {
          groups[ST2_bucketOf(s, {}).bucket].push(s);
        });
        ST2_BUCKET_ORDER.forEach(function (k) { groups[k] = ST_sessionSort(groups[k]); });
        var flat = [];
        ST2_BUCKET_ORDER.forEach(function (k) {
          groups[k].forEach(function (s) { flat.push(s.session_id); });
        });
        var out = flat.join(',');
    """)
    assert ctx.eval("out") == "z-needs,m-run,a-done"


def test_a_running_session_with_a_pending_yield_groups_under_needs() -> None:
    # This is the case the rail exists for, and it only works if the pending
    # snapshot is keyed the same way the list endpoint keys its rows.
    ctx = _ctx()
    ctx.eval("""
        var sessions = [{session_id: "s1", status: "running"}];
        var g = ST2_groupSessions(sessions, {pendingBySession: {s1: true}});
        var needs = g.needs.length, working = g.working.length;
    """)
    assert ctx.eval("needs") == 1
    assert ctx.eval("working") == 0


def test_within_group_sort_is_the_shipped_rule_not_a_reimplementation() -> None:
    src = _code_only(RUNS.read_text(encoding="utf-8"))
    assert "ST_sessionSort(" in src
    assert "function ST_sessionSort" not in src, "must reuse, not fork, the sort"
    assert ".sort(" not in src, "no second ordering rule in the rail"


# ---------------------------------------------------------------------------
# Reuse, not duplication
# ---------------------------------------------------------------------------


def test_files_rail_rehosts_filestree_and_does_not_copy_it() -> None:
    src = FILES.read_text(encoding="utf-8")
    assert "<FilesTree" in src
    assert "function FilesTree(" not in src
    # A copy would drag the context menu / upload / mount modals along with it.
    for forked in ("ST_FileContextMenu", "ST_MountCollectionModal", "ST_ApplyPreviewModal"):
        assert f"function {forked}(" not in src, forked
    assert len(src.splitlines()) < 60, "the files rail is a wrapper, not a rewrite"


def test_runs_rail_reuses_the_create_form_and_both_dialogs() -> None:
    src = RUNS.read_text(encoding="utf-8")
    for comp in ("NewSessionForm", "ST_SessionDeleteDialog", "ST_SessionRenameDialog"):
        assert f"<{comp}" in src, comp
        assert f"function {comp}(" not in src, f"{comp} must be reused, not forked"


def test_runs_rail_does_not_name_urls_directly() -> None:
    src = RUNS.read_text(encoding="utf-8")
    assert 'apiFetch("GET"' not in src
    assert "/workspaces/" not in src
    assert "ST2_api." in src


def test_rail_shares_the_sessions_cache_key_with_the_attention_bar() -> None:
    # Three components poll sessions; one key means one request.
    assert "ST2_api.keys.sessions(wid)" in RUNS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Row affordances
# ---------------------------------------------------------------------------


def test_needs_rows_carry_the_amber_left_edge() -> None:
    src = RUNS.read_text(encoding="utf-8")
    assert "isNeeds" in src
    assert "var(--amber)" in src


def test_dot_tone_uses_the_vocabulary_st_dotstyle_implements() -> None:
    # ST_dotStyle knows green-pulse/amber/red/gray; anything else silently
    # renders the inert dim dot, so a live run would look idle.
    src = RUNS.read_text(encoding="utf-8")
    assert "ST2_BUCKET_DOT" in src
    assert "green-pulse" in src
    assert 'ST_dotStyle("blue")' not in src
    dotstyle = SIDEBAR.read_text(encoding="utf-8")
    for tone in ("green-pulse", "amber", "red", "gray"):
        assert f'tone === "{tone}"' in dotstyle, tone


def test_delete_closes_the_matching_tab_in_both_panes() -> None:
    src = RUNS.read_text(encoding="utf-8")
    assert "studio.closeTab" in src
    # §5: extend the shipped close-on-delete to the companion pane, so a
    # deleted session cannot sit there 404ing.
    assert "studio.closeAsideTab" in src


def test_open_beside_is_guarded_until_the_companion_pane_exists() -> None:
    # Task 6 builds openAside. Until then the affordance must not render as a
    # button that does nothing.
    src = RUNS.read_text(encoding="utf-8")
    assert 'typeof studio.openAside === "function"' in src
    assert "onOpenAside ? (" in src


def test_cmd_click_opens_beside_when_available() -> None:
    src = RUNS.read_text(encoding="utf-8")
    assert "e.metaKey || e.ctrlKey" in src


def test_row_keeps_the_shipped_testids() -> None:
    # The existing e2e journeys locate rows by these.
    src = RUNS.read_text(encoding="utf-8")
    for tid in ("session-row", "session-status-dot", "session-rename",
                "session-delete", "new-session-btn"):
        assert f'"{tid}"' in src, tid
    assert "data-session-id={sid}" in src


# ---------------------------------------------------------------------------
# The rail shell + state
# ---------------------------------------------------------------------------


def test_rail_exposes_its_mode_and_filter_testids() -> None:
    rail = RAIL.read_text(encoding="utf-8")
    assert '"studio-rail"' in rail
    assert '"rail-mode-" + m.id' in rail
    for m in ('id: "runs"', 'id: "files"'):
        assert m in rail, m
    runs = RUNS.read_text(encoding="utf-8")
    assert '"rail-filter-" + f.id' in runs
    assert '"rail-group-" + key' in runs


def test_the_mine_filter_is_absent_because_no_field_backs_it() -> None:
    # SessionInfo has no owner/user field (session_id, agent_id, workspace_id,
    # name, status, ended_reason, parent_session_id, started_at,
    # last_activity_at, ended_at, initial_instructions), so a "mine" chip could
    # only ever lie. "Open" is the same intent over data that exists.
    from primer.model.workspace_session import SessionInfo

    fields = set(SessionInfo.model_fields)
    assert not fields & {"owner", "user_id", "created_by", "owner_id"}
    runs = _code_only(RUNS.read_text(encoding="utf-8"))
    assert '"mine"' not in runs
    assert 'id: "open"' in runs


def test_rail_state_is_persisted_and_defaulted() -> None:
    src = STUDIO.read_text(encoding="utf-8")
    assert '"railMode"' in src
    assert '"railFilter"' in src
    assert 'railMode: "runs"' in src
    assert 'railFilter: "all"' in src
    assert "setRailMode:" in src
    assert "setRailFilter:" in src


def test_v1_sidebar_still_renders_when_the_tweak_is_off() -> None:
    src = STUDIO.read_text(encoding="utf-8")
    assert "<StudioSidebar wid={wid} studio={studio} />" in src
    assert "window.StudioRail" in src
    # The column testid must not move - the drag handle and every shipped
    # journey locate the left column by it.
    assert 'data-testid="studio-sidebar"' in src


def test_files_rail_opens_the_collapsed_v1_section_once() -> None:
    # filesOpen is persisted from the two-section sidebar; in the rail Files IS
    # the mode, so a collapsed carry-over would render an empty panel.
    src = FILES.read_text(encoding="utf-8")
    assert "studio.state.filesOpen" in src
    assert "studio.toggleFiles" in src
    assert "opened.current" in src


def test_changed_by_agents_lens_is_not_faked() -> None:
    # WIRING §5 specifies it, but the turn trail it filters on needs a backend
    # endpoint that does not exist yet (plan §0.2). It must be absent, not stubbed.
    src = _code_only(FILES.read_text(encoding="utf-8"))
    assert "Changed by agents" not in src


def test_registration_order_puts_the_rail_after_the_sidebar() -> None:
    lines = (UI / "index.html").read_text(encoding="utf-8").splitlines()
    reg = [i for i, ln in enumerate(lines) if 'type="text/babel"' in ln and "src=" in ln]

    def idx(frag: str) -> int:
        for i in reg:
            if frag in lines[i]:
                return i
        raise AssertionError(f"{frag} is not registered")

    # The rail reuses FilesTree / NewSessionForm / the dialogs from the sidebar.
    assert idx("studio-sidebar.jsx") < idx("studio/st-runs-rail.jsx")
    assert idx("studio/st-runs-rail.jsx") < idx("studio/st-rail.jsx")
    assert idx("studio/st-files-rail.jsx") < idx("studio/st-rail.jsx")
    assert idx("studio/st-rail.jsx") < idx("components/studio.jsx")


def test_every_rail_module_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    for rel in ("components/studio/st-rail.jsx",
                "components/studio/st-runs-rail.jsx",
                "components/studio/st-files-rail.jsx",
                "components/studio.jsx"):
        assert b._transform((UI / rel).read_text(encoding="utf-8"), rel), rel
