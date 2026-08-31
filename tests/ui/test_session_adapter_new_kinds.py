"""The transcript handles the four kinds P1 added (S1 P6 Task 34).

This is cross-plan finding F12. P1 shipped reasoning,
external_tool_call, agent_marker and rewind_marker, and every one of
them fell through `SA_KIND_TO_TRANSCRIPT[rec.kind] || "lifecycle"` to a
generic dot row.

rewind_marker was originally skipped entirely (a structural instruction
to the replay walk, not content) - correct while nothing in the console
could trigger a rewind. US-008 R3 item 4 wires the overflow menu's
Rewind picker to the real endpoint, and the /messages read is
visible=false by design (primer/api/routers/sessions.py), so the raw
discarded rows are never hidden upstream - skipping the marker too would
leave a rewind that visibly did nothing. It now renders as a fold
divider (same treatment as compaction_marker) and SA_toTranscript hides
the span it names.

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

    def test_rewind_marker_renders_as_a_fold_divider(self):
        """US-008 R3 item 4: a wired rewind must be visible, not silent -
        it renders like compaction_marker's divider, not skipped."""
        src = _adapter()
        table = src.split("SA_SKIP_IN_TRANSCRIPT")[1].split("};")[0]
        assert "rewind_marker" not in table
        render_map = src.split("SA_KIND_TO_TRANSCRIPT = {")[1].split("};")[0]
        assert 'rewind_marker: "divider"' in render_map

    def test_rewind_hides_the_span_it_discarded(self):
        """The marker alone isn't enough - the reader must not still see
        the turns the rewind was supposed to remove.

        R3 cross-review defect 1: the fold moved from a single (to_seq,
        marker_seq) band check (didn't compose across a SECOND rewind)
        to SA_visibleRecords, a progressive walk ported from
        primer/session/replay.py's visible_records - pinned by name
        since that composition property is exactly what regressed."""
        src = _adapter()
        assert "function SA_visibleRecords(" in src
        body = src.split("function SA_toTranscript(")[1].split("\nwindow.")[0]
        assert "SA_visibleRecords(records)" in body

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
