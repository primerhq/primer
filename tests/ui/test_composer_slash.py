"""Task D2 of docs/superpowers/plans/2026-07-05-chat-refactor.md — slash
commands. Typing `/` at the start of the composer opens a discoverable
command menu; the initial set maps to existing chat actions: `/compact`
(POST /chats/{id}/compact), `/agent <name>` (POST /chats/{id}/agent),
`/new` (create a chat / navigate), `/end` (DELETE /chats/{id}). The
registry is a plain, extensible `{name, hint, run}` array.

Split across two files, same as the actions it wires up:
* ui/components/chat/composer.jsx — the menu itself (leading-"/"
  detection, prefix filtering, Enter-to-run matching). Stays a pure,
  non-fetching shell per Task B4/D1 (no `apiFetch`/`WebSocket` here) —
  it just renders and matches against whatever `slashCommands` array
  it's handed.
* ui/components/chat/conversation.jsx — builds the actual registry
  against its own existing REST actions (handleCompact already existed
  pre-D2; /agent, /new, /end reuse the same base-relative (no `/v1`)
  `apiFetch` calls CT_AgentSwitcher / ChatsPage already use in
  chats.jsx) and hands it to <Composer> via the `slashCommands` prop
  (reserved since Task B4, previously passed as `[]`).

Static-source + transpile-build checks only (the ui/ suite convention,
e.g. test_composer_attach.py) — no DOM/browser harness.
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


def test_registry_covers_the_initial_command_set() -> None:
    # The registry lives in conversation.jsx (the real REST actions);
    # composer.jsx just consumes whatever it's handed via `slashCommands`.
    src = _src(CONVERSATION)
    for name in ('name: "compact"', 'name: "agent"', 'name: "new"', 'name: "end"'):
        assert name in src, f"missing slash-command registry entry: {name}"


def test_composer_no_longer_passed_an_empty_slash_registry() -> None:
    conv = _src(CONVERSATION)
    assert "slashCommands={[]}" not in conv
    assert "slashCommands={slashCommands}" in conv


def test_menu_opens_on_a_leading_slash() -> None:
    src = _src(COMPOSER)
    assert 'startsWith("/")' in src
    assert 'data-testid="chat-slash-menu"' in src
    # The menu is filtered by whatever's typed after "/" so far, not
    # just "does it start with slash" — a discoverable, narrowing list.
    assert "slashMatches" in src and ".filter(" in src


def test_enter_matches_the_typed_word_against_the_registry_and_runs_it() -> None:
    src = _src(COMPOSER)
    assert "cmd.run(arg)" in src or "cmd.run(" in src
    assert ".toLowerCase() === draft.word.toLowerCase()" in src


def test_non_command_text_still_sends_normally() -> None:
    src = _src(COMPOSER)
    # Unmatched slash text (or plain text) falls through to the normal
    # send path rather than being swallowed.
    assert "onSend()" in src


def test_composer_stays_pure_no_direct_fetch_or_ws() -> None:
    # Same invariant Task D1 established (test_composer_attach.py) —
    # D2 must not reintroduce data-fetching into the shell; the real
    # REST calls stay in conversation.jsx behind the `run` callbacks.
    src = _src(COMPOSER)
    assert "new WebSocket(" not in src
    assert "apiFetch" not in src


def test_compact_and_agent_route_through_existing_base_relative_rest_calls() -> None:
    src = _src(CONVERSATION)
    # Base-relative — apiFetch already prefixes /v1 itself, so callers
    # never spell it out (matches every other apiFetch call in this
    # file; the WS URL a few lines down is the one legitimate spot that
    # *does* need the full /v1 path, since it's a raw WebSocket, not an
    # apiFetch call — hence checking these two exact literals rather
    # than a blanket "/v1/chats" not in src).
    assert '`/chats/${encodeURIComponent(cid)}/compact`' in src
    assert '`/chats/${encodeURIComponent(cid)}/agent`' in src
    assert '`/v1/chats/${encodeURIComponent(cid)}/compact`' not in src
    assert '`/v1/chats/${encodeURIComponent(cid)}/agent`' not in src


def test_new_and_end_reuse_existing_chat_actions() -> None:
    src = _src(CONVERSATION)
    # /end reuses the same force-delete ChatsPage's row-delete mutation
    # already uses (tests/ui or ui/components/chats.jsx precedent).
    assert '`/chats/${encodeURIComponent(cid)}?force=true`' in src
    # /new navigates via the same router hook used elsewhere (agents.jsx,
    # chats.jsx) rather than a bespoke history hack.
    assert 'useRouter' in src
    assert 'navigate("/chats")' in src


def test_bundle_transpiles_with_composer_slash_changes() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/chat/composer.jsx === */" in text
    assert "/* === components/chat/conversation.jsx === */" in text
