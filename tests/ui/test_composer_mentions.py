"""Task D3 of docs/superpowers/plans/2026-07-05-chat-refactor.md — @-mentions.
Typing `@` opens a discoverable autocomplete menu over agents / files /
sessions; selecting an entry inserts a structured `@type:id` ref token into
the message text.

Split the same way as D2's slash-command menu:
* ui/components/chat/composer.jsx — the menu itself (cursor-relative "@"
  detection, prefix filtering, keyboard nav, token insertion). Stays a
  pure, non-fetching shell per Task B4/D1/D2 (no `apiFetch`/`WebSocket`
  here) — it just renders and matches against whatever `mentionSources`
  array it's handed.
* ui/components/chat/conversation.jsx — builds the actual `mentionSources`
  array against its own existing data: agents via the same
  `GET /agents?limit=200` call + cache key `CT_AgentSwitcher` already uses,
  sessions via the chats list (`GET /chats?limit=200`, same as
  `ChatsPage`), and files via this chat's own attachments (the draft's
  pending attachments plus filenames already seen in the transcript) — and
  hands the result to <Composer> via the `mentionSources` prop (reserved
  since Task B4, previously passed as `[]`).

Static-source + transpile-build checks only (the ui/ suite convention,
e.g. test_composer_slash.py) — no DOM/browser harness.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHAT_DIR = UI / "components" / "chat"
COMPOSER = CHAT_DIR / "composer.jsx"
CONVERSATION = CHAT_DIR / "conversation.jsx"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_at_sign_opens_a_mention_menu() -> None:
    src = _src(COMPOSER)
    assert 'data-testid="chat-mention-menu"' in src
    # The trigger is cursor-relative (an "@" can appear anywhere in the
    # draft, not just at the start like "/") — not a plain startsWith.
    assert 'lastIndexOf("@")' in src


def test_mention_matches_are_prefix_filtered() -> None:
    src = _src(COMPOSER)
    assert "mentionMatches" in src and ".filter(" in src
    assert "startsWith" in src


def test_mention_menu_is_keyboard_navigable() -> None:
    src = _src(COMPOSER)
    assert "mentionActiveIndex" in src
    assert "ArrowDown" in src and "ArrowUp" in src


def test_selecting_a_mention_inserts_a_structured_ref_token() -> None:
    src = _src(COMPOSER)
    # Structured ref token, e.g. "@agent:claude-x" — built from the
    # picked item's type + id, not just its display label.
    assert "item.type" in src and "item.id" in src


def test_mention_query_is_debounced() -> None:
    src = _src(COMPOSER)
    assert "setTimeout(" in src
    assert "debouncedMention" in src


def test_composer_stays_pure_no_direct_fetch_or_ws() -> None:
    # Same invariant Task D1/D2 established — D3 must not reintroduce
    # data-fetching into the shell; the real REST calls (agents/sessions
    # lists) stay in conversation.jsx behind the `mentionSources` prop.
    src = _src(COMPOSER)
    assert "new WebSocket(" not in src
    assert "apiFetch" not in src


def test_conversation_builds_agent_file_session_mention_sources() -> None:
    src = _src(CONVERSATION)
    assert "/agents?limit=200" in src
    assert 'type: "agent"' in src
    assert 'type: "session"' in src
    assert 'type: "file"' in src


def test_composer_no_longer_passed_an_empty_mention_source_list() -> None:
    conv = _src(CONVERSATION)
    assert "mentionSources={[]}" not in conv
    assert "mentionSources={mentionSources}" in conv


def test_bundle_transpiles_with_composer_mention_changes() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/chat/composer.jsx === */" in text
    assert "/* === components/chat/conversation.jsx === */" in text
