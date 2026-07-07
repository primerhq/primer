"""Task F1 of docs/superpowers/plans/2026-07-05-chat-refactor.md (R1) —
relocate the agent selector (<CT_AgentSwitcher>, ui/components/chats.jsx)
out of the bottom-left composer row to the top-right, next to the
back/title chrome.

conversation.jsx side (still current): <Conversation> owns nothing about
the agent selector — it only knows about the opaque `headerSlot` /
`rightChromeSlot` nodes and renders them in a top-of-panel chrome row
(never inline in the composer's flex row). Those slot tests below still
hold.

SUPERSEDED for ChatDetail by C1 (fix/studio-ux, PR #114): the desktop
ChatDetail header is now a SINGLE row. The agent selector + schema toggle
+ TokenMeter moved OUT of <Conversation>'s `rightChromeSlot` and INTO
ChatDetail's own `panel-h .right` cluster, so ChatDetail now passes BOTH
slots as `null` and <Conversation>'s second chrome row no longer renders.
The overlay + ⌘/Ctrl+Shift+A shortcut behavior lives in
test_agent_switch_overlay.py.

Static-source + transpile-build checks only (the ui/ suite convention,
e.g. test_conversation_extracted.py / test_composer_schema_shells.py) —
no DOM/browser harness.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHAT_DIR = UI / "components" / "chat"
CONVERSATION = CHAT_DIR / "conversation.jsx"
CHATS = UI / "components" / "chats.jsx"


def _conv_src() -> str:
    return CONVERSATION.read_text(encoding="utf-8")


def _chats_src() -> str:
    return CHATS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# <Conversation> — the switcher must be gone from the composer row, and the
# generic `rightChromeSlot`/`headerSlot` nodes must render up top instead.
# ---------------------------------------------------------------------------


def test_conversation_no_longer_renders_agent_switcher_jsx() -> None:
    src = _conv_src()
    # Prose comments may still reference the identifier for context (e.g.
    # "mirror CT_AgentSwitcher / ChatsPage in chats.jsx", or "<CT_AgentSwitcher>"
    # written prose-style with an immediate closing `>`) — what must be gone
    # is the actual JSX render, which spans multiple lines of props (real
    # element usage puts a newline right after the tag name, not `>`).
    assert "<CT_AgentSwitcher\n" not in src, (
        "<Conversation> must not render <CT_AgentSwitcher> directly — "
        "the host (chats.jsx) supplies it opaquely via `rightChromeSlot`"
    )


def test_conversation_still_accepts_header_and_right_chrome_slots() -> None:
    src = _conv_src()
    assert "headerSlot" in src
    assert "rightChromeSlot" in src


def test_right_chrome_slot_renders_above_the_transcript_not_in_composer() -> None:
    src = _conv_src()
    assert "{rightChromeSlot}" in src, "<Conversation> must render `rightChromeSlot` somewhere"
    idx_slot = src.index("{rightChromeSlot}")
    # `<Transcript` / `<Composer` (no trailing `>`/`'`) each appear once in
    # prose comments earlier in the file — the actual JSX element renders
    # with a newline immediately after the tag name, so anchor on that to
    # find the real mount points rather than the comment mentions.
    idx_transcript = src.index("<Transcript\n")
    idx_composer = src.index("<Composer\n")
    assert idx_slot < idx_transcript, (
        "`rightChromeSlot` must render in the top-of-panel chrome row, "
        "before <Transcript> — not down near the composer"
    )
    assert idx_slot < idx_composer, (
        "`rightChromeSlot` must render before <Composer> — it left the "
        "bottom composer row entirely"
    )


def test_header_and_right_chrome_slots_render_in_the_same_chrome_row() -> None:
    src = _conv_src()
    idx_header = src.index("{headerSlot}")
    idx_right = src.index("{rightChromeSlot}")
    # Anchor: both opaque host slots render close together, in the same
    # top-of-panel row (not one at the top and one buried near the
    # composer/Send control further down the file).
    assert abs(idx_right - idx_header) < 600


def test_composer_row_no_longer_has_agent_switcher_placement_up() -> None:
    # The old bottom-composer switcher popped "up" (composer sits at the
    # bottom). That placement override is gone along with the switcher.
    src = _conv_src()
    assert 'placement="up"' not in src


# ---------------------------------------------------------------------------
# chats.jsx (ChatDetail, the host) — C1: the desktop header is ONE row now.
# The agent selector lives in ChatDetail's own `panel-h .right` cluster, so
# BOTH of <Conversation>'s opaque chrome slots are `null` (its second header
# row no longer renders).
# ---------------------------------------------------------------------------


def test_chat_detail_passes_null_chrome_slots_for_one_row_header() -> None:
    # C1: with the switcher moved into ChatDetail's own header row, both
    # opaque slots must be null so <Conversation> renders no second row.
    src = _chats_src()
    assert "<Conversation\n" in src
    start = src.index("<Conversation\n")
    end = src.index("/>", start)
    block = src[start:end]
    assert "rightChromeSlot={null}" in block, (
        "C1: ChatDetail must pass `rightChromeSlot={null}` — the agent "
        "selector moved into its own one-row header cluster"
    )
    assert "headerSlot={null}" in block, (
        "C1: ChatDetail must pass `headerSlot={null}` so <Conversation>'s "
        "second chrome row no longer renders"
    )


def test_chat_detail_agent_switcher_lives_in_the_one_row_header_cluster() -> None:
    # C1/C2 + Fix 2: <CT_AgentSwitcher> is grouped with the schema toggle +
    # the compact button inside ChatDetail's `chat-header-cluster`, ABOVE the
    # <Conversation> mount — no longer inside <Conversation>'s
    # `rightChromeSlot`. Fix 2 replaced the in-cluster <TokenMeter> with a
    # purpose-built compact button (`chat-compact-btn`) that carries the token
    # count; the shared <TokenMeter> now only appears in the mobile kebab
    # sheet further down the file.
    src = _chats_src()
    conv_start = src.index("<Conversation\n")
    conv_end = src.index("/>", conv_start)
    conv_block = src[conv_start:conv_end]
    assert "<CT_AgentSwitcher" not in conv_block, (
        "C1: the agent switcher must NOT be wired into <Conversation> "
        "anymore — it lives in ChatDetail's own header cluster"
    )
    cluster = src.index('data-testid="chat-header-cluster"')
    switcher = src.index("<CT_AgentSwitcher")
    schema = src.index('data-testid="chat-schema-panel-toggle"')
    compact = src.index('data-testid="chat-compact-btn"')
    # The cluster opens first, then the three grouped controls, all before
    # the <Conversation> mount further down the file.
    assert cluster < switcher < schema < compact < conv_start, (
        "C1/C2 + Fix 2: agent selector + schema toggle + compact button must "
        "be grouped in that order inside the `chat-header-cluster`, above "
        "<Conversation>"
    )


def test_chat_detail_agent_switcher_still_defined() -> None:
    src = _chats_src()
    assert "function CT_AgentSwitcher(" in src
    assert "window.ChatDetail = ChatDetail;" in src


# ---------------------------------------------------------------------------
# Transpile
# ---------------------------------------------------------------------------


def test_bundle_still_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/chat/conversation.jsx === */" in text
    assert "/* === components/chats.jsx === */" in text
