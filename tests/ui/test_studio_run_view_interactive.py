"""Task 12 of docs/superpowers/plans/2026-07-05-studio-agents-interact.md —
the agent run view (`SessionAgentPanel` in `ui/components/studio-center.jsx`)
rebuilt as a session-backed `<Conversation>`: the reused
`window.Transcript`/`window.Composer` chat-refactor primitives fed by the
session adapter (Task 11, `ui/components/session-adapter.jsx`), with a
Stop/End/Restart control set instead of the graph panel's Pause/Steer/Cancel.

Static-source + transpile-build checks only (the `tests/ui` harness
convention — see test_studio_center.py / test_studio_terminal.py /
test_session_adapter.py), plus two MiniRacer evals of the actual pure
helper functions this task adds (`ST_isAutonomous`, `ST_sessionTranscriptRows`
+ friends) so the load-bearing control-set derivation and row-flattening
logic are exercised for real rather than only substring-matched — mirroring
test_session_adapter.py's `test_sa_to_transcript_maps_records_via_mini_racer`.

No browser/live server: <Transcript>/<Composer> are full React components
with live EventSource/WS-adjacent effects (session-adapter.jsx's tap SSE)
that only a real DOM could exercise end-to-end; driving those interactively
here would need the heavier tests/ui_e2e Playwright harness (a live server +
PRIMER_RUN_UI_E2E=1), which this task's file path + run command
(`tests/ui/test_studio_run_view_interactive.py`, plain
`pytest ... -n0 -p no:cacheprovider`) does not provision. The control-set
WIRING (Stop -> .../interrupt, End -> .../cancel, Restart -> .../restart) is
therefore asserted the same way test_studio_terminal.py asserts its WS URL/
control-frame protocol: substring checks scoped to the exact source region
that wires each control, so a future edit that silently drops/rewires one of
these three endpoints fails a specific, named assertion.
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


def _session_agent_panel_src() -> str:
    """Just the `SessionAgentPanel` function body — scopes assertions (like
    "no dedicated steer button") to this panel without false-matching
    `SessionGraphPanel`'s still-inlined `ST_SessionControls` (which does have
    a Steer button; the graph panel is untouched by this task)."""
    return _fn_block(_center_src(), "function SessionAgentPanel(", "function SessionGraphPanel(")


def _pure_helpers_src() -> str:
    """The plain-JS helper functions this task adds ahead of
    `SessionAgentPanel` itself (ST_isAutonomous, ST_sessionRowToTranscript,
    ST_coalesceAssistantRows, ST_sessionTranscriptRows) — no JSX, so (like
    session-adapter.jsx) this slice is directly `py_mini_racer`-evaluable
    without a full Babel/React/DOM stack."""
    return _fn_block(_center_src(), "function ST_isAutonomous(", "function SessionAgentPanel(")


# ---------------------------------------------------------------------------
# SessionAgentPanel now composes the reused chat-refactor primitives over
# the session adapter — not SessionLiveStream.
# ---------------------------------------------------------------------------


def test_session_agent_panel_uses_session_adapter_and_transcript_composer() -> None:
    panel = _session_agent_panel_src()
    assert "window.SA_useSessionConversation(" in panel
    assert "ST_sessionTranscriptRows(" in panel
    assert "<window.Transcript" in panel
    assert "<window.Composer" in panel
    # No bespoke chat UI (per the brief) — Message rows + the input surface
    # are the reused components, not a second parallel renderer.
    assert "function Transcript(" not in panel
    assert "function Composer(" not in panel


def test_session_agent_panel_no_longer_reuses_session_live_stream() -> None:
    # SessionLiveStream backed the OLD (pre-Task-11/12) agent panel; Task 12
    # explicitly retires that reuse path for THIS panel (SessionGraphPanel /
    # the graph run-view, Task 13, is untouched and reuses a different
    # production component, SD_GraphRunView).
    panel = _session_agent_panel_src()
    assert "SessionLiveStream" not in panel


def test_transcript_fed_by_sa_to_transcript_via_the_flattening_helper() -> None:
    src = _center_src()
    helpers = _pure_helpers_src()
    # ST_sessionTranscriptRows is the "fed by SA_toTranscript" pipeline the
    # brief describes: SA_toTranscript's rows (Task 11, payload stays
    # nested there by design) flattened + assistant-token-coalesced into
    # what <Transcript>'s Message() row renderer actually reads.
    assert "window.SA_toTranscript(records, session)" in src
    assert "function ST_sessionRowToTranscript(" in helpers
    assert "function ST_coalesceAssistantRows(" in helpers
    assert "function ST_sessionTranscriptRows(" in helpers


# ---------------------------------------------------------------------------
# No dedicated steer button — steering IS sending a message (brief §4.2).
# ---------------------------------------------------------------------------


def test_no_dedicated_steer_button_on_the_agent_panel() -> None:
    panel = _session_agent_panel_src()
    assert "ctrl-steer" not in panel
    assert "ST_SessionControls" not in panel


def test_session_graph_panel_steer_control_is_untouched() -> None:
    # Sanity: the graph panel's existing Steer/Pause/Resume/Cancel cluster
    # (ST_SessionControls) is unaffected by this task — it still exists
    # exactly once elsewhere in the file.
    src = _center_src()
    assert src.count("function ST_SessionControls(") == 1
    assert 'data-testid="ctrl-steer"' in src


# ---------------------------------------------------------------------------
# Interactive control set: Stop -> .../interrupt, End -> .../cancel,
# Restart (status === "ended" only) -> .../restart.
# ---------------------------------------------------------------------------


def test_composer_stop_wired_to_adapter_stop_which_hits_interrupt() -> None:
    panel = _session_agent_panel_src()
    assert "onStop={onStop}" in panel
    assert "conv.stop()" in panel
    # The endpoint itself is session-adapter.jsx's contract (also covered by
    # test_session_adapter.py::test_controls_hit_the_documented_endpoints);
    # re-asserted here so this file alone documents the full Stop path.
    assert "/interrupt" in _adapter_src()


def test_end_control_wired_to_adapter_end_which_hits_cancel() -> None:
    panel = _session_agent_panel_src()
    assert 'data-testid="ctrl-end"' in panel
    assert "endMut.mutate()" in panel
    assert "conv.end()" in panel
    # End disables once the session has already ended — nothing left to cancel.
    assert "disabled={!wid || isEnded || endMut.loading}" in panel
    assert "/cancel" in _adapter_src()


def test_restart_control_gated_on_ended_status_and_hits_restart() -> None:
    panel = _session_agent_panel_src()
    assert 'data-testid="ctrl-restart"' in panel
    # Gated behind the same `isEnded` used for the header + Composer's
    # disabled prop, and `isEnded` is derived from `status === "ended"`
    # exactly (per brief §Interface — not the broader SESSION_TERMINAL set).
    restart_gate = _fn_block(panel, "{isEnded && (", 'data-testid="ctrl-restart"')
    assert "Btn" in restart_gate
    assert 'status === "ended"' in panel
    assert "restartMut.mutate()" in panel
    assert "conv.restart()" in panel
    assert "/restart" in _adapter_src()


def test_no_pause_control_on_the_agent_panel() -> None:
    panel = _session_agent_panel_src()
    assert "ctrl-pause" not in panel
    assert "Pause" not in panel


# ---------------------------------------------------------------------------
# Adapter extension: restart() + wsState were not part of Task 11's original
# interface — this task adds them (session-adapter.jsx), minimally.
# ---------------------------------------------------------------------------


def test_adapter_restart_extension() -> None:
    src = _adapter_src()
    assert "var restart = React.useCallback(" in src
    assert '"/restart"' in src
    assert "restart: restart," in src
    # The locked Task 11 mapping/exports are untouched.
    assert "window.SA_toTranscript = SA_toTranscript;" in src
    assert "window.SA_KIND_TO_TRANSCRIPT = SA_KIND_TO_TRANSCRIPT;" in src
    assert "window.SA_useSessionConversation = SA_useSessionConversation;" in src


def test_adapter_wsstate_extension_for_transcript_connection_pill() -> None:
    src = _adapter_src()
    assert "wsState: wsState," in src
    assert 'es.onopen = function () { setWsState("open"); };' in src


# ---------------------------------------------------------------------------
# Non-invoked (CREATED) session: empty stream + composer prompting for the
# first input — no separate "invoke" affordance, first Send auto-wakes it.
# ---------------------------------------------------------------------------


def test_first_send_goes_through_sendmessage_no_separate_invoke_control() -> None:
    panel = _session_agent_panel_src()
    assert "conv.sendMessage(text)" in panel
    assert "ctrl-invoke" not in panel
    assert "onSend={onSend}" in panel


# ---------------------------------------------------------------------------
# ST_isAutonomous — mirrors primer/session/autonomy.py::session_is_autonomous
# exactly: explicit `session.autonomous` wins; else binding kind.
# ---------------------------------------------------------------------------


def test_st_is_autonomous_mirrors_backend_derivation_via_mini_racer() -> None:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(_pure_helpers_src())

    assert ctx.eval('ST_isAutonomous({binding: {kind: "graph"}})') is True
    assert ctx.eval('ST_isAutonomous({binding: {kind: "agent"}})') is False
    # Explicit flag wins over binding kind, either direction.
    assert ctx.eval('ST_isAutonomous({binding: {kind: "agent"}, autonomous: true})') is True
    assert ctx.eval('ST_isAutonomous({binding: {kind: "graph"}, autonomous: false})') is False
    # Defensive: no session / no binding.
    assert ctx.eval("ST_isAutonomous(null)") is False
    assert ctx.eval("ST_isAutonomous({})") is False


# ---------------------------------------------------------------------------
# ST_sessionTranscriptRows — flattens SA_toTranscript's rows for <Message>
# (tool_result call_id->id / output->result aliasing; per-token
# assistant_message coalescing with startSeq/endSeq).
# ---------------------------------------------------------------------------


def test_session_transcript_rows_flattens_and_coalesces_via_mini_racer() -> None:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(_adapter_src())  # defines window.SA_toTranscript
    ctx.eval(_pure_helpers_src())  # defines ST_sessionTranscriptRows + friends
    ctx.eval(
        """
        var records = [
          {seq: 1, kind: "user_input", payload: {text: "hi"}, created_at: "t1", node_id: null},
          {seq: 2, kind: "assistant_token", payload: {text: "Hel"}, created_at: "t2", node_id: null},
          {seq: 3, kind: "assistant_token", payload: {text: "lo!"}, created_at: "t3", node_id: null},
          {seq: 4, kind: "tool_call",
           payload: {id: "call-1", arguments: {path: "a.txt"}}, created_at: "t4", node_id: null},
          {seq: 5, kind: "tool_result",
           payload: {call_id: "call-1", output: "contents", error: null}, created_at: "t5", node_id: null},
          {seq: 6, kind: "done", payload: {stop_reason: "end_turn"}, created_at: "t6", node_id: null},
        ];
        var out = ST_sessionTranscriptRows(records, {id: "s1"});
        """
    )
    # Two per-token assistant_message rows (seq 2, 3) coalesce into one.
    assert ctx.eval("out.length") == 5

    assert ctx.eval("out[0].kind") == "user_message"
    assert ctx.eval("out[0].text") == "hi"

    assert ctx.eval("out[1].kind") == "assistant_message"
    assert ctx.eval("out[1].text") == "Hello!"
    assert ctx.eval("out[1].startSeq") == 2
    assert ctx.eval("out[1].endSeq") == 3

    assert ctx.eval("out[2].kind") == "tool_call"
    assert ctx.eval("out[2].id") == "call-1"

    assert ctx.eval("out[3].kind") == "tool_result"
    # Aliased so <Transcript> pairs tool_call<->tool_result by `.id`.
    assert ctx.eval("out[3].id") == "call-1"
    # Aliased from the session payload's `output` -> Message()'s `.result`.
    assert ctx.eval("out[3].result") == "contents"

    assert ctx.eval("out[4].kind") == "lifecycle"
    # No dedicated Message() branch for a collapsed "lifecycle" row; falls
    # back to a payload field instead of rendering a blank bubble.
    assert ctx.eval("out[4].text") == "end_turn"


def test_divider_rows_seed_text_from_the_divider_label() -> None:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(_adapter_src())
    ctx.eval(_pure_helpers_src())
    ctx.eval(
        """
        var records = [
          {seq: 1, kind: "invocation_divider", payload: {invocation: 3}, created_at: "t1", node_id: null},
        ];
        var out = ST_sessionTranscriptRows(records, {id: "s1"});
        """
    )
    assert ctx.eval("out[0].kind") == "divider"
    assert ctx.eval("out[0].text") == "— invocation 3 —"


# ---------------------------------------------------------------------------
# index.html load order: session-adapter.jsx (window.SA_* / SA_useSession-
# Conversation) and the chat-refactor primitives must both be registered —
# already true (Task 11 + chat-refactor); re-asserted here since Task 12 is
# the first file to actually reference window.Transcript/window.Composer
# from studio-center.jsx.
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
# and the extended session-adapter.jsx compiles).
# ---------------------------------------------------------------------------


def test_bundle_transpiles_with_the_rebuilt_agent_run_view() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/studio-center.jsx === */" in text
    assert "/* === components/session-adapter.jsx === */" in text
