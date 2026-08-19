"""M13: llm_call is trace-tab material, never a transcript row.

Paged message reads still return it (the REST endpoint is untouched); it
is the renderer that skips it, so an unmapped kind cannot fall through to
the generic "lifecycle" bubble.
"""
from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui" / "components"
SRC = UI / "session-adapter.jsx"
FRAME = UI / "session-frame.jsx"


def test_files_exist() -> None:
    assert SRC.exists()
    assert FRAME.exists()


def test_llm_call_joins_the_one_skip_registry() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "var SA_SKIP_IN_TRANSCRIPT = " in src
    table = src[src.index("var SA_SKIP_IN_TRANSCRIPT = ") :]
    table = table[: table.index("};")]
    assert "llm_call: true" in table
    assert "client_action: true" in table, "S3's entry must survive"


def test_there_is_no_second_skip_registry() -> None:
    """One concept, one table: S3 Task 9 owns SA_SKIP_IN_TRANSCRIPT."""
    src = SRC.read_text(encoding="utf-8")
    assert "SA_HIDDEN_KINDS" not in src
    assert src.count("var SA_SKIP_IN_TRANSCRIPT = ") == 1


def test_hidden_kinds_are_skipped_in_the_mapper() -> None:
    src = SRC.read_text(encoding="utf-8")
    body = src[src.index("function SA_toTranscript") :]
    body = body[: body.index("\n}")]
    assert "SA_SKIP_IN_TRANSCRIPT[rec.kind]" in body
    assert body.count("SA_SKIP_IN_TRANSCRIPT[rec.kind]") == 1
    assert "continue" in body


def test_llm_call_is_not_mapped_to_a_transcript_kind() -> None:
    src = SRC.read_text(encoding="utf-8")
    table = src[src.index("var SA_KIND_TO_TRANSCRIPT") :]
    table = table[: table.index("};")]
    assert "llm_call" not in table


def test_the_registry_is_exported_to_window() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "window.SA_SKIP_IN_TRANSCRIPT" in src


def test_frame_renderer_drops_llm_call_before_the_unknown_kind_fallback() -> None:
    """The second transcript renderer (session-detail + node inspector).

    Without a guard, session-frame.jsx's trailing "unknown / future frame
    kinds" branch renders every model call as a dim mono line.
    """
    src = FRAME.read_text(encoding="utf-8")
    body = src[src.index("function _SLS_Frame") :]
    body = body[: body.index("\n}\n")]
    guard = body.index('kind === "llm_call"')
    fallback = body.index("Unknown / future frame kinds")
    assert guard < fallback, "the guard must precede the catch-all"
    assert "return null" in body[guard : guard + 120]
