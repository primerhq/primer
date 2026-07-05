"""Task B2 of docs/superpowers/plans/2026-07-05-chat-refactor.md —
extract the WS/data lifecycle + optimistic-echo out of ChatDetail
(ui/components/chats.jsx) into a self-contained <Conversation> core
(ui/components/chat/conversation.jsx) that Studio can later embed, plus
shared flat/coalesce helpers (ui/components/chat/use-transcript.js).

Static-source + transpile-build checks only (the ui/ suite convention,
e.g. test_graph_canvas_extracted.py / test_highlight_code_vendor.py) —
no DOM/browser harness.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHAT_DIR = UI / "components" / "chat"
CONVERSATION = CHAT_DIR / "conversation.jsx"
USE_TRANSCRIPT = CHAT_DIR / "use-transcript.js"
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


def test_conversation_module_exists_and_exports() -> None:
    assert CONVERSATION.exists(), "ui/components/chat/conversation.jsx is missing"
    src = CONVERSATION.read_text(encoding="utf-8")
    assert "function Conversation(" in src
    assert "window.Conversation = Conversation;" in src


def test_conversation_accepts_the_documented_props() -> None:
    src = CONVERSATION.read_text(encoding="utf-8")
    for prop in ("chatId", "headerSlot", "rightChromeSlot", "showSchemaPanel", "onStatus"):
        assert prop in src, f"<Conversation> must accept `{prop}`"


def test_conversation_owns_the_ws_lifecycle() -> None:
    src = CONVERSATION.read_text(encoding="utf-8")
    assert "new WebSocket(" in src, "the WS connection must live in <Conversation> now"
    assert "SENTINEL_TAIL_SEQ" in src, "the tail-load must live in <Conversation> now"
    assert "sendMessage" in src


def test_conversation_has_no_viewport_relative_height() -> None:
    src = CONVERSATION.read_text(encoding="utf-8")
    # §3: <Conversation> fills its flex parent (height:100%/flex:1); the
    # HOST owns any vh/dvh viewport sizing, not the embeddable core.
    assert "100vh" not in src
    assert "100dvh" not in src


def test_chats_jsx_mounts_conversation_and_no_longer_owns_the_raw_socket() -> None:
    src = CHATS.read_text(encoding="utf-8")
    assert "<Conversation" in src, "ChatDetail must mount <Conversation> as its data/transcript core"
    assert "new WebSocket(" not in src, (
        "the raw WebSocket connection must have moved out of chats.jsx into "
        "ui/components/chat/conversation.jsx"
    )


def test_chats_jsx_exposes_a_reachable_schema_panel_toggle() -> None:
    """R3's <SchemaPanel> (Task F2) was fully built but ChatDetail hard-coded
    `showSchemaPanel={false}` with no toggle, so operators could never reach
    it. A "schema" toggle now lives in the same top-right chrome slot as the
    agent selector (R1) and drives real state through to <Conversation>.
    """
    src = CHATS.read_text(encoding="utf-8")
    assert "const [showSchemaPanel, setShowSchemaPanel] = React.useState(false);" in src
    assert 'data-testid="chat-schema-panel-toggle"' in src
    assert "onClick={() => setShowSchemaPanel((v) => !v)}" in src
    assert "showSchemaPanel={showSchemaPanel}" in src
    assert "showSchemaPanel={false}" not in src, (
        "ChatDetail must no longer hard-code showSchemaPanel to false"
    )


def test_conversation_schema_effects_are_gated_on_show_schema_panel() -> None:
    """The schema-hydration effect and the debounced persist effect used to
    run unconditionally even with the panel hidden, silently re-PUTting
    Chat.response_format ~500ms after load. Both must bail out early when
    the panel isn't shown.
    """
    src = CONVERSATION.read_text(encoding="utf-8")
    assert "if (!showSchemaPanel) return;" in src
    assert "if (!showSchemaPanel) return undefined;" in src


def test_use_transcript_exports_flatten_and_coalesce() -> None:
    assert USE_TRANSCRIPT.exists(), "ui/components/chat/use-transcript.js is missing"
    src = USE_TRANSCRIPT.read_text(encoding="utf-8")
    assert "window.chatFlatten = chatFlatten;" in src
    assert "window.chatCoalesce = chatCoalesce;" in src


def test_chat_coalesce_forwards_agent_id_and_created_at_from_first_token() -> None:
    """Task C2 fold-in fix: C1 already threads `agent_id`/`created_at`
    through REST-loaded history (read straight off the persisted
    ChatMessage row), but a LIVE-STREAMED reply only carries those fields
    on the raw `assistant_token` frames that `chatCoalesce` merges away —
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


def test_conversation_uses_shared_helpers() -> None:
    src = CONVERSATION.read_text(encoding="utf-8")
    assert "window.chatFlatten(" in src
    assert "window.chatCoalesce(" in src


def test_new_chat_scripts_registered_before_chats_jsx() -> None:
    order = _order()
    assert "components/chat/use-transcript.js" in order
    assert "components/chat/conversation.jsx" in order
    assert order.index("components/chat/use-transcript.js") < order.index("components/chats.jsx")
    assert order.index("components/chat/conversation.jsx") < order.index("components/chats.jsx")


def test_bundle_transpiles_with_new_chat_files() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/chat/use-transcript.js === */" in text
    assert "/* === components/chat/conversation.jsx === */" in text
