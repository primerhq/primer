"""Task 11 of docs/superpowers/plans/2026-07-05-studio-agents-interact.md —
session adapter mapping a workspace Session's message stream
(SessionMessageKind / the tap's mirrored TapEventClass) onto the shape
chat-refactor's `<Transcript>` renders, so a Session can be rendered
through the reused chat UI instead of a second parallel renderer.

Static-source + transpile-build checks only (the ui/ suite convention,
e.g. test_conversation_extracted.py / test_session_live_history.py), plus
one MiniRacer eval of the actual mapping function (mirrors
test_chat_coalesce_forwards_agent_id_and_created_at_from_first_token) so
the load-bearing kind table and divider labels are exercised for real
rather than only substring-matched.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
ADAPTER = UI / "components" / "session-adapter.jsx"
INDEX = UI / "index.html"


def _order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_session_adapter_module_exists_and_exports() -> None:
    assert ADAPTER.exists(), "ui/components/session-adapter.jsx is missing"
    src = ADAPTER.read_text(encoding="utf-8")
    assert "function SA_toTranscript(" in src
    assert "window.SA_toTranscript = SA_toTranscript;" in src
    assert "window.SA_KIND_TO_TRANSCRIPT = SA_KIND_TO_TRANSCRIPT;" in src
    assert "function SA_useSessionConversation(" in src
    assert "window.SA_useSessionConversation = SA_useSessionConversation;" in src


def test_kind_mapping_table_matches_the_locked_contract() -> None:
    # Load-bearing mapping (studio-agents-interact Task 11 + Global
    # Constraints' "Transport rules (locked)"): every SessionMessageKind /
    # TapEventClass value must appear with its documented transcript kind.
    src = ADAPTER.read_text(encoding="utf-8")
    expected = {
        "user_input": "user_message",
        "assistant_token": "assistant_message",
        "tool_call": "tool_call",
        "tool_result": "tool_result",
        "graph_transition": "divider",
        "invocation_divider": "divider",
        "yielded": "interaction",
        "resumed": "interaction",
        "done": "lifecycle",
        "cancelled": "lifecycle",
        "error": "lifecycle",
    }
    for kind, transcript_kind in expected.items():
        assert f'{kind}: "{transcript_kind}"' in src, (
            f"SA_KIND_TO_TRANSCRIPT must map {kind!r} -> {transcript_kind!r}"
        )


def test_no_session_websocket_only_rest_and_tap_sse() -> None:
    # Transport rule (locked): reuse the workspace tap SSE, never a
    # dedicated session WebSocket.
    src = ADAPTER.read_text(encoding="utf-8")
    assert "new WebSocket(" not in src
    assert "new EventSource(" in src
    assert "/tap" in src
    assert "/messages" in src


def test_controls_hit_the_documented_endpoints() -> None:
    src = ADAPTER.read_text(encoding="utf-8")
    assert "/steer" in src
    assert "/interrupt" in src
    assert "/cancel" in src


def test_session_scoped_tap_reuses_wtp_build_selector() -> None:
    # Reuse components/workspace-tap.jsx's selector builder to scope the
    # live tail to this one session, rather than re-deriving the
    # TapSelector predicate shape here.
    src = ADAPTER.read_text(encoding="utf-8")
    assert "window.WTP_buildSelector" in src


def test_session_adapter_registered_before_studio_center() -> None:
    order = _order()
    assert "components/session-adapter.jsx" in order
    assert "components/studio-center.jsx" in order
    assert order.index("components/session-adapter.jsx") < order.index("components/studio-center.jsx")


def test_bundle_transpiles_with_session_adapter() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/session-adapter.jsx === */" in text


def test_sa_to_transcript_maps_records_via_mini_racer() -> None:
    """Runs the real SA_toTranscript/SA_KIND_TO_TRANSCRIPT against a tiny
    sample of SessionMessageRecord-shaped rows, mirroring
    test_chat_coalesce_forwards_agent_id_and_created_at_from_first_token's
    use of py_mini_racer instead of guessing at behavior from a substring
    match.
    """
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(ADAPTER.read_text(encoding="utf-8"))
    ctx.eval(
        """
        var records = [
          {seq: 1, kind: "user_input", payload: {text: "hi"}, created_at: "t1", node_id: null},
          {seq: 2, kind: "graph_transition",
           payload: {node_id: "n1", phase: "enter"}, created_at: "t2", node_id: "n1"},
          {seq: 3, kind: "invocation_divider", payload: {invocation: 3}, created_at: "t3", node_id: null},
          {seq: 4, kind: "done", payload: {}, created_at: "t4", node_id: null},
        ];
        var out = window.SA_toTranscript(records, {id: "s1"});
        """
    )
    assert ctx.eval("out.length") == 4
    assert ctx.eval("out[0].kind") == "user_message"
    assert ctx.eval("out[1].kind") == "divider"
    assert ctx.eval("out[1].label") == "n1 · enter"
    assert ctx.eval("out[1].nodeId") == "n1"
    assert ctx.eval("out[2].kind") == "divider"
    assert ctx.eval("out[2].label") == "— invocation 3 —"
    assert ctx.eval("out[3].kind") == "lifecycle"
