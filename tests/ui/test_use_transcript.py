"""``shared/use-transcript.js`` exports the transcript helpers.

Lifted from test_conversation_extracted.py when S1 P7 deleted the chat
UI. The helpers themselves survive: session-detail.jsx, the shell session doc
and the studio attention panel all render transcripts through them.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
USE_TRANSCRIPT = UI / "components" / "shared" / "use-transcript.js"


def test_use_transcript_exports_flatten_and_coalesce() -> None:
    assert USE_TRANSCRIPT.exists(), "ui/components/shared/use-transcript.js is missing"
    src = USE_TRANSCRIPT.read_text(encoding="utf-8")
    assert "window.chatFlatten = chatFlatten;" in src
    assert "window.chatCoalesce = chatCoalesce;" in src

def test_chat_coalesce_forwards_agent_id_and_created_at_from_first_token() -> None:
    """Task C2 fold-in fix: C1 already threads `agent_id`/`created_at`
    through REST-loaded history (read straight off the persisted
    ChatMessage row), but a LIVE-STREAMED reply only carries those fields
    on the raw `assistant_token` frames that `chatCoalesce` merges away -
    without forwarding them onto the synthetic assistant_message, a
    live reply's attribution label + timestamp went missing until the
    next full reload. Runs the actual helper via py_mini_racer rather
    than guessing at the shape from a substring match (mirrors
    test_highlight_code_vendor.py's use of the same harness).
    """
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(USE_TRANSCRIPT.read_text(encoding="utf-8"))
    ctx.eval(
        """
        var tokens = [
          {kind: "assistant_token", seq: 5, delta: "Hel",
           agent_id: "researcher", created_at: "2026-07-05T00:00:00Z"},
          {kind: "assistant_token", seq: 6, delta: "lo",
           agent_id: "researcher", created_at: "2026-07-05T00:00:01Z"},
        ];
        var out = window.chatCoalesce(tokens);
        """
    )
    assert ctx.eval("out.length") == 1
    assert ctx.eval("out[0].kind") == "assistant_message"
    assert ctx.eval("out[0].text") == "Hello"
    assert ctx.eval("out[0].agent_id") == "researcher"
    # created_at comes from the FIRST token of the run, not the last.
    assert ctx.eval("out[0].created_at") == "2026-07-05T00:00:00Z"
