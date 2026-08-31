"""S7 section 6: the Trace panel is shell-agnostic and props-only."""
from __future__ import annotations

from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[2]
    / "ui" / "components" / "shared" / "session-trace.jsx"
)
INDEX = Path(__file__).resolve().parents[2] / "ui" / "index.html"


def test_file_exists() -> None:
    assert SRC.exists()


def test_component_defined_and_exported() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "function SessionTracePanel" in src
    assert "window.SessionTracePanel" in src


def test_reads_the_timeline_endpoint() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "/turns/" in src and "/timeline" in src


def test_renders_the_trace_affordances() -> None:
    src = SRC.read_text(encoding="utf-8")
    for token in ("llm_call", "tool_call", "node", "duration_ms",
                  "input_tokens", "output_tokens", "waits"):
        assert token in src, f"missing {token}"


def test_is_props_only_no_routes_or_location() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "window.location" not in src
    assert "ROUTES" not in src


def test_has_a_testid() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert 'data-testid="session-trace"' in src


def test_registered_in_index_html() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert "components/shared/session-trace.jsx" in html
