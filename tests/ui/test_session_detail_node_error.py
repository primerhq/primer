"""When a SessionMessageRecord's payload carries `error` (NodeOutput.error from
a failed graph node — emitted by the workspace executor as either an ``error``
frame from ``_GraphErrorEvent`` or an ``assistant_token`` enriched with the
node's failure metadata), session-detail.jsx surfaces it as a prominent red
"ERROR" badge with the message in a wrapped <pre> block, and an additional
subtler grey chip for the structured ``ended_detail`` code when distinct.

Spec B §5 / Phase 10.1.
"""

from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "session-detail.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_error_badge_rendered() -> None:
    src = _src()
    # The renderer must reach into payload.error (flattened to m.error by the
    # WS frame normalisation, but the grep covers both shapes for safety).
    assert "payload?.error" in src or "payload.error" in src or "m.error" in src
    # The badge must visibly use the project's red colour token.
    assert "var(--red" in src
    # The literal ERROR label must appear so the operator can grep the UI.
    assert ">ERROR<" in src


def test_ended_detail_code_chip_rendered() -> None:
    src = _src()
    # The structured failure code (NodeOutput.ended_detail, persisted as
    # payload.code on the _GraphErrorEvent frame) renders as a subtler chip.
    assert "ended_detail" in src or "code:" in src or '"code:"' in src
