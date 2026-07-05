"""Task C3 of docs/superpowers/plans/2026-07-05-chat-refactor.md — hybrid
tool-call rendering (D4): pair a `tool_call` with its later `tool_result`
by shared `id`; a still-running (unpaired) call renders expanded; a
completed (paired) call collapses to a one-line result chip
(`name(key-arg) ✓ 2.1s`) that re-expands on click; tool payloads render
via `window.primerVendor.highlightCode(text, "json")` instead of a raw
`JSON.stringify` + `word-break:break-all` dump.

Static-source + transpile-build checks only (the ui/ suite convention,
e.g. test_transcript_timeline.py / test_turn_anatomy.py) — no
DOM/browser harness.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
TRANSCRIPT = UI / "components" / "chat" / "transcript.jsx"


def _src() -> str:
    return TRANSCRIPT.read_text(encoding="utf-8")


def test_tool_call_and_tool_result_rows_are_paired_by_id() -> None:
    src = _src()
    assert 'row.kind === "tool_result" && row.id' in src
    assert 'row.kind === "tool_call" && row.id' in src
    assert "toolResultsById" in src
    assert "toolCallIdsPresent" in src
    # A paired tool_result folds into its tool_call's row rather than
    # rendering as its own second row.
    assert "toolCallIdsPresent.has(m.id)" in src


def test_running_unpaired_tool_call_renders_expanded() -> None:
    src = _src()
    assert "pairedResult" in src
    assert "defaultOpen={true}" in src


def test_completed_tool_call_collapses_to_a_chip_with_duration() -> None:
    src = _src()
    assert "defaultOpen={false}" in src
    assert "CT_toolDuration" in src
    assert "CT_formatDuration" in src
    assert "✓" in src
    assert "✗" in src


def test_tool_payload_uses_highlighted_code_not_a_raw_dump() -> None:
    src = _src()
    assert 'highlightCode(fullText' in src
    assert 'wordBreak: "break-all"' not in src


def test_bundle_transpiles_with_hybrid_tool_rendering() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/chat/transcript.jsx === */" in text
