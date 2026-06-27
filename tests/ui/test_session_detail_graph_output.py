"""The session-detail live stream renders End structured output (parsed
payload) as a collapsible block under the assistant message."""

from __future__ import annotations
from pathlib import Path

# The structured-output renderer (_SLS_Frame / _SLS_coalesceMessages) was
# extracted into the shared session-frame.jsx module; assert against it there.
SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "session-frame.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_renders_payload_parsed_block() -> None:
    src = _src()
    assert "Structured output" in src or "structured_output" in src
    assert "parsed" in src
