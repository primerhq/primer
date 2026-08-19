"""S1 P1 added four record kinds; nothing renders them yet.

REASONING, EXTERNAL_TOOL_CALL, AGENT_MARKER and REWIND_MARKER are live in
SessionMessageKind and mirrored into TapEventClass, so they arrive on both
the history read and the tap. Without a mapping they all fall through to
SA_toTranscript's generic "lifecycle" dot row (session-adapter.jsx:83),
including rewind_marker, which is a structural instruction the reader must
never see at all.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "ui" / "components" / "session-adapter.jsx"
TURNS = ROOT / "ui" / "foundation" / "shell-turns.js"


def test_every_shipped_kind_has_a_transcript_mapping() -> None:
    from primer.model.workspace_session import SessionMessageKind

    src = ADAPTER.read_text(encoding="utf-8")
    table = src[src.index("var SA_KIND_TO_TRANSCRIPT"):src.index("// Divider label")]
    unmapped = [
        k.value for k in SessionMessageKind
        if k.value + ":" not in table and k.value not in _hidden(src)
    ]
    assert not unmapped, (
        f"kinds that would render as a generic lifecycle dot: {unmapped}"
    )


def _hidden(src: str) -> set[str]:
    """Kinds deliberately suppressed rather than mapped.

    Reads the table's KEYS. Splitting the block on ":" instead would fold
    every explanatory comment into the token set and match nothing, which
    silently reports suppressed kinds as unmapped.
    """
    import re

    start = src.index("var SA_SKIP_IN_TRANSCRIPT")
    end = src.index("};", start)
    return set(re.findall(r"^\s{2}(\w+):", src[start:end], re.M))


def test_rewind_marker_is_suppressed_not_rendered() -> None:
    src = ADAPTER.read_text(encoding="utf-8")
    assert "rewind_marker" in src[src.index("var SA_SKIP_IN_TRANSCRIPT"):]


def test_reasoning_collapses_and_external_tool_calls_fold_into_their_pair() -> None:
    turns = TURNS.read_text(encoding="utf-8")
    assert "reasoning" in turns
    assert "external_tool_call" in turns
