"""Structural-presence checks for the Studio center region (PR-B / B3).

The center region is the tab bar + the active document panel. B3 builds:
  - CenterTabs        — tab bar over studio.state.openTabs
  - SessionAgentPanel — agent transcript (session adapter + Transcript/Composer)
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
# REUSE — the graph run-view is the production component from session-detail.jsx;
# the agent transcript is now chat-refactor's Transcript/Composer over the
# session adapter (Task 12) — both reached as window globals (NOT reimplemented).
# ---------------------------------------------------------------------------


def test_agent_panel_uses_session_adapter_and_chat_primitives() -> None:
    # Task 12 retired the pre-Task-12 SessionLiveStream reuse for the agent
    # panel (it deliberately no longer reuses window.SessionLiveStream); the
    # panel now composes the session adapter + the reused chat primitives.
    # (The graph run-view still reuses SD_GraphRunView — see below.)
    src = _center_src()
    assert "window.SessionLiveStream" not in src, "agent panel must NOT reuse SessionLiveStream"
    assert "window.SA_useSessionConversation(" in src, "must drive the panel off the session adapter"
    assert "window.SA_toTranscript(records, session)" in src, "transcript must be fed by SA_toTranscript"
    assert "<window.Transcript" in src, "must reuse the chat <Transcript> primitive"
    assert "<window.Composer" in src, "must reuse the chat <Composer> primitive"


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
# Session panel routing: fetch the session, branch on ST_isAutonomous
# (explicit session.autonomous wins; else the binding kind).
# ---------------------------------------------------------------------------


def test_session_panel_fetches_and_branches_on_binding_kind() -> None:
    src = _center_src()
    assert "/sessions/" in src, "must GET /v1/sessions/{ref}"
    assert "useResource" in src
    # Branch agent vs graph via ST_isAutonomous, which itself reads binding.kind
    # (with the defensive binding_kind alias).
    assert "binding.kind" in src
    assert "graph" in src and "agent" in src


def _session_panel_src() -> str:
    """Just the ST_SessionPanel router body — scopes the routing assertions
    to the resolver, not the whole file."""
    src = _center_src()
    start = src.index("function ST_SessionPanel(")
    end = src.index("function ST_FilePreview(", start)
    return src[start:end]


def _is_autonomous_src() -> str:
    """The pure ST_isAutonomous helper (no JSX) — directly MiniRacer-evaluable
    like test_studio_run_view_interactive.py's `_pure_helpers_src`."""
    src = _center_src()
    start = src.index("function ST_isAutonomous(")
    end = src.index("function ST_sessionRowToTranscript(", start)
    return src[start:end]


def test_session_panel_routes_through_st_is_autonomous() -> None:
    # ST_isAutonomous is the byte-mirror of backend session_is_autonomous:
    # an explicit `session.autonomous` flag wins over the binding kind. The
    # router MUST gate agent-vs-graph on it (not the raw `binding.kind ===
    # "graph"`), or an explicit override that contradicts the binding kind
    # routes to the WRONG panel.
    router = _session_panel_src()
    assert "ST_isAutonomous(session)" in router, "router must branch on ST_isAutonomous"
    # The old router re-derived `var kind = ... binding.kind ...` and branched
    # on `kind === "graph"`; that derivation must be gone (the check lives in
    # ST_isAutonomous now). Asserts on the removed code, not the prose comment.
    assert "var kind =" not in router, "router must not re-derive the raw binding kind to branch on"
    assert "<SessionGraphPanel" in router and "<SessionAgentPanel" in router


def test_explicit_override_session_routes_per_st_is_autonomous() -> None:
    # Prove the override semantics the router now delegates to: an explicit
    # `autonomous` flag decides the panel regardless of binding.kind. The
    # router (asserted above) renders the graph/autonomous panel iff
    # ST_isAutonomous(session) is truthy, else SessionAgentPanel.
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(_is_autonomous_src())

    # agent binding + explicit autonomous:true -> ST_isAutonomous true ->
    # SessionGraphPanel (override wins over binding.kind).
    assert ctx.eval('ST_isAutonomous({binding: {kind: "agent"}, autonomous: true})') is True
    # graph binding + explicit autonomous:false -> ST_isAutonomous false ->
    # SessionAgentPanel (override wins over binding.kind).
    assert ctx.eval('ST_isAutonomous({binding: {kind: "graph"}, autonomous: false})') is False
    # No override: behaves exactly like the old `binding.kind === "graph"`
    # branch, so non-override routing is unchanged.
    assert ctx.eval('ST_isAutonomous({binding: {kind: "graph"}})') is True
    assert ctx.eval('ST_isAutonomous({binding: {kind: "agent"}})') is False


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
