"""Task C1 of docs/superpowers/plans/2026-07-05-chat-refactor.md —
redesign <Transcript> (ui/components/chat/transcript.jsx) into the
single-column agent-timeline: per-message agent attribution (`m.agent_id`,
stamped by backend Task A4) + timestamp (`m.created_at`), first-class
marker rows for `agent_marker` (switch/handoff/joined) and `cancelled`
(Task A6), alongside the existing `compaction_marker` divider.

Static-source + transpile-build checks only (the ui/ suite convention,
e.g. test_transcript_extracted.py / test_highlight_code_vendor.py) — no
DOM/browser harness.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
TRANSCRIPT = UI / "components" / "chat" / "transcript.jsx"


def _src() -> str:
    return TRANSCRIPT.read_text(encoding="utf-8")


def test_agent_marker_kind_branches_and_renders_switch_handoff_joined() -> None:
    src = _src()
    assert 'kind === "agent_marker"' in src
    # §4.1 / plan Task C1 label text per `marker` value.
    assert "switched to" in src
    assert "handoff" in src
    assert "joined" in src
    assert "⇄" in src
    assert "▶" in src


def test_cancelled_is_a_first_class_marker_row_not_the_generic_dot() -> None:
    src = _src()
    # The old catch-all lumped `cancelled` in with yielded/resumed/done as
    # a plain "· <kind>" dot; C1 promotes it to its own marker-row branch
    # (Task A6 backend support).
    assert (
        '"yielded" || kind === "resumed" || kind === "done" || kind === "cancelled"'
        not in src
    ), "cancelled must no longer share the generic yielded/resumed/done branch"
    assert 'kind === "cancelled"' in src


def test_attribution_reads_agent_id_not_a_hardcoded_agent_literal() -> None:
    src = _src()
    # The old hardcoded ternary label this replaces — per-message
    # attribution must show the real producing agent id instead.
    assert '{isUser ? "user" : "agent"}' not in src
    assert "m.agent_id" in src


def test_timestamp_field_is_rendered() -> None:
    src = _src()
    assert "m.created_at" in src
    assert "toLocaleTimeString" in src


def test_bundle_transpiles_with_updated_transcript() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/chat/transcript.jsx === */" in text
