"""Shared rewind / compact / schema controls (S1 P6 Task 35).

Props-only like the binding controls, so the next shell re-hosts rather
than rewrites.

The two rejections a user can act on are surfaced as their own
messages, not a generic error: a busy session, and a rewind target
inside compacted history (amendment C2). Everything else is a toast.

When a session is parked, abandoning the gate and rewinding are TWO
explicit calls. Chaining them silently would discard an agent's pending
work on the way to something the user asked for separately.
"""

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
MODULE = UI / "components" / "shared" / "session-history-controls.jsx"


def _src() -> str:
    return MODULE.read_text(encoding="utf-8")


def _code_only() -> str:
    return "\n".join(
        line for line in _src().splitlines()
        if not line.strip().startswith("//")
    )


def test_module_exists_and_exports_the_global():
    assert "window.SessionHistoryControls" in _src()


def test_index_loads_it():
    text = (UI / "index.html").read_text(encoding="utf-8")
    assert 'src="components/shared/session-history-controls.jsx"' in text


def test_props_only_contract():
    code = _code_only()
    for banned in ("window.location", "ROUTES", "primerApi.useRouter"):
        assert banned not in code, f"{banned} breaks the props-only contract"


def test_calls_all_three_p2_endpoints():
    code = _code_only()
    assert "/rewind" in code
    assert "/compact" in code
    assert "/response_format" in code


def test_busy_and_compacted_rejections_are_distinguished():
    """Both are 409, and a single "conflict" message would leave the
    user unable to tell which one they hit."""
    src = _src()
    assert "busy" in src.lower()
    assert "compact" in src.lower()


def test_parked_rewind_is_two_explicit_calls():
    """Abandoning a gate throws away pending agent work, so it is never
    a silent step inside another action."""
    code = _code_only()
    assert "abandon" in code.lower()


def test_reuses_the_shared_schema_panel():
    assert "SchemaPanel" in _code_only()


def test_the_compaction_boundary_sentinel_is_gone():
    """Real compaction_marker rows are in the transcript now, so the
    client no longer guesses where the boundary is."""
    doc = (UI / "components" / "console" / "nv-session-doc.jsx").read_text(
        encoding="utf-8"
    )
    assert "compactionBoundarySeq" not in doc
