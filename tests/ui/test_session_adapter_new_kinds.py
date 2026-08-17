"""The transcript handles the four kinds P1 added (S1 P6 Task 34).

This is cross-plan finding F12. P1 shipped reasoning,
external_tool_call, agent_marker and rewind_marker, and every one of
them fell through `SA_KIND_TO_TRANSCRIPT[rec.kind] || "lifecycle"` to a
generic dot row.

rewind_marker is the worst of the four: it is a structural instruction
to the replay walk, not content, and must not be visible at all.

The skip table is deliberately ONE table. Findings F26 and F36 found S3
and S7 each planning their own registry at this same insertion point;
S1 arrives first, so it establishes SA_SKIP_IN_TRANSCRIPT and the later
specs extend it rather than adding a second.
"""

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _adapter() -> str:
    return (UI / "components" / "session-adapter.jsx").read_text(
        encoding="utf-8"
    )


def _transcript() -> str:
    return (UI / "components" / "shared" / "transcript.jsx").read_text(
        encoding="utf-8"
    )


class TestSkipTable:
    def test_one_named_skip_table_exists(self):
        assert "SA_SKIP_IN_TRANSCRIPT" in _adapter()

    def test_rewind_marker_is_skipped_not_rendered(self):
        """A structural instruction must never reach the reader."""
        src = _adapter()
        table = src.split("SA_SKIP_IN_TRANSCRIPT")[1][:300]
        assert "rewind_marker" in table

    def test_rewind_marker_is_not_in_the_render_map(self):
        src = _adapter()
        render_map = src.split("SA_KIND_TO_TRANSCRIPT = {")[1].split("};")[0]
        assert "rewind_marker" not in render_map

    def test_the_skip_is_applied_in_the_conversion_loop(self):
        """A table nothing consults would be decoration."""
        src = _adapter()
        body = src.split("function SA_toTranscript(")[1].split("\n}")[0]
        assert "SA_SKIP_IN_TRANSCRIPT" in body


class TestRenderedKinds:
    def test_reasoning_maps_to_its_own_row(self):
        src = _adapter()
        render_map = src.split("SA_KIND_TO_TRANSCRIPT = {")[1].split("};")[0]
        assert "reasoning" in render_map

    def test_agent_marker_maps_to_a_binding_row(self):
        src = _adapter()
        render_map = src.split("SA_KIND_TO_TRANSCRIPT = {")[1].split("};")[0]
        assert "agent_marker" in render_map

    def test_external_tool_call_maps_to_the_tool_rendering(self):
        """Folded into the paired call/result, not a third row."""
        src = _adapter()
        render_map = src.split("SA_KIND_TO_TRANSCRIPT = {")[1].split("};")[0]
        assert "external_tool_call" in render_map

    def test_transcript_renders_reasoning_and_binding_rows(self):
        src = _transcript()
        assert "reasoning" in src
        assert "agent_marker" in src or "binding_change" in src


class TestHandoffNote:
    def test_the_s8_handoff_is_recorded_in_the_adapter(self):
        """S8 replaces this renderer and its plan names none of these
        four kinds, so the decisions have to travel with the code."""
        src = _adapter()
        assert "S8" in src
