"""Task 13 of docs/superpowers/plans/2026-07-05-studio-agents-interact.md —
the graph run view (`SessionGraphPanel` in `ui/components/studio-center.jsx`)
rebuilt as a toggleable `window.SD_GraphRunView` G6 canvas over the SAME
session-backed `<Transcript>`/`<Composer>` the agent panel (Task 12) uses,
with an AUTONOMOUS Pause/Cancel/Restart control set instead of Stop/End.

Static-source + transpile-build checks only — same rationale
test_studio_run_view_interactive.py documents for Task 12: `<Transcript>`/
`<Composer>`/`SD_GraphRunView` are full React components with live
EventSource/G6-canvas effects that only a real DOM could exercise, and
driving those interactively needs the heavier tests/ui_e2e Playwright
harness (a live server + PRIMER_RUN_UI_E2E=1), which this task's file path
+ run command (plain `pytest tests/ui/test_studio_graph_run_view.py -n0
-p no:cacheprovider`) does not provision. The viz-toggle's show/hide
behavior, the graph_transition->divider mapping, and the Pause/Cancel/
Restart (not Stop/End) wiring are therefore asserted the same way
test_studio_terminal.py/test_studio_run_view_interactive.py assert their
protocols: substring + scoped-slice checks pinned to the exact source
region that wires each control, plus two MiniRacer evals of the actual
pure helper functions (`ST_sessionTranscriptRows` via `SA_toTranscript`)
so the "graph-transition records render as dividers" acceptance criterion
is exercised for real rather than only substring-matched.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CENTER = UI / "components" / "studio-center.jsx"
ADAPTER = UI / "components" / "session-adapter.jsx"
INDEX = UI / "index.html"


def _center_src() -> str:
    return CENTER.read_text(encoding="utf-8")


def _adapter_src() -> str:
    return ADAPTER.read_text(encoding="utf-8")


def _fn_block(src: str, start_marker: str, end_marker: str) -> str:
    """Slice `src` from `start_marker` up to (not including) `end_marker`."""
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


def _session_graph_panel_src() -> str:
    """Just the `SessionGraphPanel` function body — scopes assertions to
    this panel without false-matching `SessionAgentPanel`'s Stop/End set."""
    return _fn_block(_center_src(), "function SessionGraphPanel(", "function ST_SessionPanel(")


# ---------------------------------------------------------------------------
# SessionGraphPanel composes the SAME reused chat-refactor primitives over
# the session adapter that the agent panel (Task 12) uses — not a second
# parallel transcript renderer.
# ---------------------------------------------------------------------------


def test_session_graph_panel_uses_session_adapter_and_transcript_composer() -> None:
    panel = _session_graph_panel_src()
    assert "window.SA_useSessionConversation(" in panel
    assert "ST_sessionTranscriptRows(" in panel
    assert "<window.Transcript" in panel
    assert "<window.Composer" in panel
    assert "function Transcript(" not in panel
    assert "function Composer(" not in panel


def test_session_graph_panel_still_reuses_sd_graph_run_view_not_reimplemented() -> None:
    panel = _session_graph_panel_src()
    assert "window.SD_GraphRunView" in panel
    assert "function SD_GraphRunView(" not in panel
    # gid from the binding, rid = the session id (a graph run IS the
    # session) — pinned exactly like test_studio_center.py's
    # test_reuses_sd_graph_run_view, scoped here to this panel alone.
    assert "gid={gid}" in panel
    assert "rid={sid}" in panel


# ---------------------------------------------------------------------------
# Viz toggle (S6): a <SD_GraphRunView> region gated on a showViz boolean
# defaulting ON, with a dedicated toggle control — OFF collapses to chat
# only, and the Transcript/Composer render regardless of the toggle state.
# ---------------------------------------------------------------------------


def test_viz_toggle_control_exists_and_defaults_on() -> None:
    panel = _session_graph_panel_src()
    assert 'data-testid="ctrl-toggle-viz"' in panel
    assert "React.useState(true)" in panel
    assert "[showViz, setShowViz]" in panel
    assert "setShowViz(function (v) { return !v; })" in panel


def test_viz_region_is_conditionally_rendered_but_chat_is_not() -> None:
    panel = _session_graph_panel_src()
    # The SD_GraphRunView region is gated behind `showViz &&`.
    assert "{showViz && (" in panel
    viz_block = _fn_block(panel, "{showViz && (", "<window.Transcript")
    assert "window.SD_GraphRunView" in viz_block
    assert 'data-testid="graph-viz-region"' in viz_block
    # Toggling off must NOT also hide the chat: <Transcript>/<Composer> sit
    # OUTSIDE the showViz-gated block entirely (after it in source order,
    # with no dependency on `showViz` themselves).
    after_viz_block = panel.split(viz_block, 1)[1]
    assert "<window.Transcript" in after_viz_block
    assert "<window.Composer" in after_viz_block


# ---------------------------------------------------------------------------
# graph_transition records render as node/phase dividers in the transcript
# — SA_toTranscript (Task 11, untouched) already maps this; exercised for
# real via MiniRacer through the SAME ST_sessionTranscriptRows pipeline the
# graph panel feeds <Transcript> with.
# ---------------------------------------------------------------------------


def test_graph_transition_records_render_as_dividers_via_mini_racer() -> None:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(_adapter_src())  # defines window.SA_toTranscript
    helpers = _fn_block(_center_src(), "function ST_isAutonomous(", "function SessionAgentPanel(")
    ctx.eval(helpers)  # defines ST_sessionTranscriptRows + friends
    ctx.eval(
        """
        var records = [
          {seq: 1, kind: "graph_transition",
           payload: {node_id: "drafter", node_kind: "agent", phase: "enter", status: "running"},
           created_at: "t1", node_id: "drafter"},
          {seq: 2, kind: "graph_transition",
           payload: {node_id: "drafter", node_kind: "agent", phase: "exit", status: "ended"},
           created_at: "t2", node_id: "drafter"},
        ];
        var out = ST_sessionTranscriptRows(records, {id: "s1", binding: {kind: "graph"}});
        """
    )
    assert ctx.eval("out.length") == 2
    assert ctx.eval("out[0].kind") == "divider"
    assert ctx.eval("out[0].text") == "drafter · enter"
    assert ctx.eval("out[0].nodeId") == "drafter"
    assert ctx.eval("out[1].kind") == "divider"
    assert ctx.eval("out[1].text") == "drafter · exit"


# ---------------------------------------------------------------------------
# Autonomous control set: Pause + Cancel, Restart once ended. NO Stop/End —
# those are the interactive (agent) set's terms; this panel never calls
# conv.stop()/interrupt.
# ---------------------------------------------------------------------------


def test_pause_control_wired_to_the_pause_endpoint() -> None:
    panel = _session_graph_panel_src()
    assert 'data-testid="ctrl-pause"' in panel
    assert "onClick={function () { if (wid) pauseMut.mutate(); }}" in panel
    assert '"/pause"' in panel


def test_cancel_control_wired_to_adapter_end_which_hits_cancel() -> None:
    panel = _session_graph_panel_src()
    assert 'data-testid="ctrl-cancel"' in panel
    assert "onClick={function () { if (wid) cancelMut.mutate(); }}" in panel
    assert "function () { return conv.end(); }" in panel
    assert "/cancel" in _adapter_src()


def test_restart_control_gated_on_ended_status_and_hits_restart() -> None:
    panel = _session_graph_panel_src()
    assert 'data-testid="ctrl-restart"' in panel
    restart_gate = _fn_block(panel, "{isEnded && (", 'data-testid="ctrl-restart"')
    assert "Btn" in restart_gate
    assert 'status === "ended"' in panel
    assert "onClick={function () { if (wid) restartMut.mutate(); }}" in panel
    assert "function () { return conv.restart(); }" in panel
    assert "/restart" in _adapter_src()


def test_no_stop_or_end_control_on_the_graph_panel() -> None:
    panel = _session_graph_panel_src()
    assert "ctrl-end" not in panel
    assert "ctrl-stop" not in panel
    assert ">Stop<" not in panel
    assert ">End<" not in panel


def test_composer_never_shows_stop_and_never_calls_interrupt() -> None:
    panel = _session_graph_panel_src()
    # running is hardcoded false, so <Composer> can never swap to its Stop
    # affordance; the panel never references conv.stop() (the interactive
    # Stop path) or a raw /interrupt call.
    assert "running={false}" in panel
    assert "conv.stop" not in panel
    assert "/interrupt" not in panel


def test_no_dedicated_steer_or_resume_button_on_the_graph_panel() -> None:
    # The graph panel's own Pause/Cancel/Restart set is self-contained; it
    # has no Steer or Resume control (those lived on the pre-Task-13 cluster,
    # since removed).
    panel = _session_graph_panel_src()
    assert "ctrl-steer" not in panel
    assert "ctrl-resume" not in panel


# ---------------------------------------------------------------------------
# S7: mounting the panel never fires pause/steer/interrupt — every mutate()
# call is wired exclusively behind an onClick, never inside a bare/mount
# effect. Each wiring string above is pinned as occurring exactly once, so
# it cannot ALSO appear un-gated inside a React.useEffect body.
# ---------------------------------------------------------------------------


def test_mutations_only_fire_from_onclick_never_on_mount() -> None:
    panel = _session_graph_panel_src()
    for call, onclick in (
        ("pauseMut.mutate()", "onClick={function () { if (wid) pauseMut.mutate(); }}"),
        ("cancelMut.mutate()", "onClick={function () { if (wid) cancelMut.mutate(); }}"),
        ("restartMut.mutate()", "onClick={function () { if (wid) restartMut.mutate(); }}"),
    ):
        assert panel.count(call) == 1, f"{call} should be wired exactly once (via its onClick)"
        assert onclick in panel


# ---------------------------------------------------------------------------
# index.html load order — unchanged by this task, re-asserted for
# self-containedness (mirrors test_studio_run_view_interactive.py).
# ---------------------------------------------------------------------------


def test_index_html_registers_chat_primitives_and_session_adapter() -> None:
    text = INDEX.read_text(encoding="utf-8")
    for src in (
        "components/session-adapter.jsx",
        "components/chat/transcript.jsx",
        "components/chat/composer.jsx",
        "components/studio-center.jsx",
    ):
        assert src in text, f"{src} missing from index.html"


# ---------------------------------------------------------------------------
# Bundle transpile (the hard gate: the whole bundle incl. studio-center.jsx
# compiles).
# ---------------------------------------------------------------------------


def test_bundle_transpiles_with_the_rebuilt_graph_run_view() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/studio-center.jsx === */" in text
    assert "/* === components/session-adapter.jsx === */" in text
