"""Task B3 of docs/superpowers/plans/2026-07-05-chat-refactor.md —
extract the single-column agent-timeline out of <Conversation>
(ui/components/chat/conversation.jsx) into a pure renderer,
<Transcript> (ui/components/shared/transcript.jsx). The row renderers
(Message, CT_ExpandableToolRow, CT_AttachmentPart, CompactionMarker,
CT_ThinkingBubble) move into the new file with it.

Static-source + transpile-build checks only (the ui/ suite convention,
e.g. test_conversation_extracted.py / test_highlight_code_vendor.py) —
no DOM/browser harness.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHAT_DIR = UI / "components" / "chat"
SHARED_DIR = UI / "components" / "shared"
TRANSCRIPT = SHARED_DIR / "transcript.jsx"
CONVERSATION = CHAT_DIR / "conversation.jsx"
CHATS = UI / "components" / "chats.jsx"
INDEX = UI / "index.html"


def _order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_transcript_module_exists_and_exports() -> None:
    assert TRANSCRIPT.exists(), "ui/components/shared/transcript.jsx is missing"
    src = TRANSCRIPT.read_text(encoding="utf-8")
    assert "function Transcript(" in src
    assert "window.Transcript = Transcript;" in src


def test_transcript_accepts_the_documented_props() -> None:
    src = TRANSCRIPT.read_text(encoding="utf-8")
    for prop in (
        "messages",
        "chatId",
        "agentId",
        "wsState",
        "waitingForReply",
        "turnStatus",
        "pendingToolCall",
        "onRewind",
        "scrollRef",
        "onScroll",
        "loadingOlder",
        "hasMoreOlder",
    ):
        assert prop in src, f"<Transcript> must accept `{prop}`"


def test_transcript_contains_the_row_renderers() -> None:
    src = TRANSCRIPT.read_text(encoding="utf-8")
    assert "function Message(" in src
    assert "function CT_ExpandableToolRow(" in src
    assert "function CT_AttachmentPart(" in src
    assert "function CompactionMarker(" in src
    assert "function CT_ThinkingBubble(" in src


def test_transcript_is_pure_no_data_fetching_or_ws() -> None:
    src = TRANSCRIPT.read_text(encoding="utf-8")
    assert "new WebSocket(" not in src
    assert "apiFetch" not in src


def test_bundle_transpiles_with_transcript_file() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/shared/transcript.jsx === */" in text
