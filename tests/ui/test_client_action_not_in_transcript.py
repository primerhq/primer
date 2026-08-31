"""The delivery record never becomes a transcript row (S3 spec section 6).

Spec section 6: a notifying call is the ordinary tool_call/tool_result
pair, and no special message KIND is introduced there. The client_action
record exists only so delivery can ride the existing tap (section 4), so
the session adapter must drop it: SA_toTranscript's default bucket is
"lifecycle", which would otherwise render a stray dot beside the pair.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "ui" / "components" / "session-adapter.jsx"


def test_skip_table_is_declared_and_exported() -> None:
    src = ADAPTER.read_text(encoding="utf-8")
    assert "var SA_SKIP_IN_TRANSCRIPT = " in src
    assert "client_action: true" in src
    assert "window.SA_SKIP_IN_TRANSCRIPT = SA_SKIP_IN_TRANSCRIPT;" in src
    # It must NOT be mapped to a transcript kind: dropping and mapping are
    # different answers and only one of them matches the spec.
    assert 'client_action: "' not in src


def test_transcript_drops_the_delivery_row_and_keeps_the_pair() -> None:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(ADAPTER.read_text(encoding="utf-8"))
    ctx.eval(
        """
        var records = [
          {seq: 1, kind: "tool_call",
           payload: {id: "tc-1", name: "client__open_file",
                     arguments: {path: "config.yaml"}},
           created_at: "t1", node_id: null},
          {seq: 2, kind: "client_action",
           payload: {call_id: "tc-1", name: "client__open_file",
                     arguments: {path: "config.yaml"}},
           created_at: "t2", node_id: null},
          {seq: 3, kind: "tool_result",
           payload: {call_id: "tc-1", output: '{"delivered": true}',
                     error: false},
           created_at: "t3", node_id: null},
        ];
        var out = window.SA_toTranscript(records, {id: "s1"});
        """
    )
    assert ctx.eval("out.length") == 2, "the delivery row must be dropped"
    assert ctx.eval("out[0].kind") == "tool_call"
    assert ctx.eval("out[1].kind") == "tool_result"
    assert ctx.eval("out[0].seq") == 1
    assert ctx.eval("out[1].seq") == 3
