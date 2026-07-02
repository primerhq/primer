"""Structural checks for the Studio live-stream consolidation batch.

Covers the field-test fixes:
  • #4  ONE shared workspace-tap EventSource per Studio view (fe-review N4).
        A module-level hub in foundation/use-workspace-tap.js owns the single
        EventSource; ActionRequired, WorkspaceTap and the graph run-view read
        from it instead of each opening their own.
  • #5  Workspace-activity rows expand to show the full event payload
        (data-testid activity-event / activity-event-detail).
  • #3/#7  The embedded live stream loads a bounded tail (tail=1) of history +
        a "Load earlier" control, not the whole messages.jsonl (limit=1000).
  • #10 The embedded SessionLiveStream is pure content — the Interrupt button
        and header chrome were removed; controls live only in the panel header.

Static-source checks (no React rendering), matching test_studio_activity.py.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
HOOK = UI / "foundation" / "use-workspace-tap.js"
ACTIVITY = UI / "components" / "studio-activity.jsx"
TAP = UI / "components" / "workspace-tap.jsx"
DETAIL = UI / "components" / "session-detail.jsx"
CENTER = UI / "components" / "studio-center.jsx"
INDEX = UI / "index.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _slice(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


# ---------------------------------------------------------------------------
# #4 — shared workspace-tap hub (one EventSource)
# ---------------------------------------------------------------------------

def test_shared_hook_exists_and_exports() -> None:
    assert HOOK.exists(), "foundation/use-workspace-tap.js missing"
    src = _read(HOOK)
    assert "window.useWorkspaceTap = useWorkspaceTap;" in src
    assert "window.useWorkspaceTapListener = useWorkspaceTapListener;" in src


def test_shared_hook_has_exactly_one_eventsource() -> None:
    # The whole point of the consolidation: the workspace-wide tap is opened in
    # exactly ONE place (the shared hub).
    assert _read(HOOK).count("new EventSource") == 1


def test_shared_hook_registered_in_index() -> None:
    order = [
        line[line.index('src="') + 5 : line.index('"', line.index('src="') + 5)]
        for line in _read(INDEX).splitlines()
        if 'type="text/babel"' in line and "src=" in line
    ]
    assert "foundation/use-workspace-tap.js" in order
    # Must load before the components that consume it.
    hook_idx = order.index("foundation/use-workspace-tap.js")
    assert hook_idx < order.index("components/workspace-tap.jsx")
    assert hook_idx < order.index("components/studio-activity.jsx")
    assert hook_idx < order.index("components/session-detail.jsx")


def test_action_required_uses_shared_listener_not_own_eventsource() -> None:
    src = _read(ACTIVITY)
    assert "useWorkspaceTapListener" in src
    assert "new EventSource" not in src


def test_workspace_tap_uses_shared_hook_not_own_eventsource() -> None:
    src = _read(TAP)
    assert "useWorkspaceTap(" in src
    assert "new EventSource" not in src


def test_graph_run_view_uses_shared_listener_not_own_eventsource() -> None:
    # The graph run-view's transition-refetch trigger reads the shared hub
    # instead of opening a third EventSource. Scope to SD_GraphRunView only —
    # SD_NodeInspector (which follows) keeps its own node-scoped, cursor-seam
    # tap deliberately, and is not part of the workspace-wide consolidation.
    body = _slice(_read(DETAIL), "function SD_GraphRunView(", "const SD_NODE_KIND_HINT")
    assert "useWorkspaceTapListener" in body
    assert "new EventSource" not in body


def test_right_rail_opens_zero_independent_eventsources() -> None:
    # Neither right-rail component constructs its own EventSource anymore.
    assert _read(ACTIVITY).count("new EventSource") == 0
    assert _read(TAP).count("new EventSource") == 0


# ---------------------------------------------------------------------------
# #5 — expandable workspace-activity events
# ---------------------------------------------------------------------------

def test_activity_events_are_expandable() -> None:
    src = _read(TAP)
    assert 'data-testid="activity-event"' in src
    assert 'data-testid="activity-event-detail"' in src
    # Accessible, keyboard-toggleable affordance, collapsed by default.
    assert "aria-expanded" in src
    assert "toggleExpand" in src
    # The full payload detail is rendered from the whole event.
    assert "WTP_detailJson" in src
    # The e2e summary-row testid is preserved for existing journeys.
    assert 'data-testid="tap-event-row"' in src


# ---------------------------------------------------------------------------
# #3 / #7 — bounded tail history + lazy older paging
# ---------------------------------------------------------------------------

def test_live_stream_loads_bounded_tail_not_whole_history() -> None:
    body = _slice(_read(DETAIL), "function SessionLiveStream(", "window.SessionLiveStream = SessionLiveStream;")
    assert "SLS_HISTORY_PAGE" in _read(DETAIL)
    assert "tail=1" in body, "initial history load must request a bounded tail"
    assert "limit=1000" not in body, "SessionLiveStream must not pull the whole log"
    # Older rows load on demand.
    assert 'data-testid="load-earlier"' in body
    assert "loadEarlier" in body


# ---------------------------------------------------------------------------
# #10 — embedded stream is pure content; controls live only in the header
# ---------------------------------------------------------------------------

def test_interrupt_button_removed_from_embedded_stream() -> None:
    body = _slice(_read(DETAIL), "function SessionLiveStream(", "window.SessionLiveStream = SessionLiveStream;")
    assert ">Interrupt<" not in body
    assert "sendInterrupt" not in body
    assert "cancelMut" not in body
    # No in-stream "Live stream" header chrome / TokenMeter.
    assert "panel-h" not in body
    assert "window.TokenMeter" not in body


def test_session_controls_still_owned_by_panel_header() -> None:
    src = _read(CENTER)
    for testid in ("ctrl-pause", "ctrl-resume", "ctrl-steer", "ctrl-cancel"):
        assert f'data-testid="{testid}"' in src


# ---------------------------------------------------------------------------
# Full bundle still transpiles with the new foundation module + edits
# ---------------------------------------------------------------------------

def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === foundation/use-workspace-tap.js === */" in text
