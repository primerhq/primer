"""Toolset cards prefer the shared OAuth sign-in state when one exists."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
AUTH = ROOT / "ui" / "components" / "toolsets" / "mcp-auth.jsx"
PLATFORM = ROOT / "ui" / "components" / "console" / "nv-platform.jsx"

PRELUDE = r"""
function PK_Section() { return null; } function PK_Segmented() { return null; }
function PK_Grid() { return null; } function PK_Field() { return null; }
function PK_Fold() { return null; } function TS_KvEditor() { return null; }
"""


@pytest.fixture
def auth():
    ctx = mini_react_context(transpile(AUTH), PRELUDE)
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("connected", {"label": "Connected", "tone": "ok"}),
        ("needs_sign_in", {"label": "Needs sign-in", "tone": "warn"}),
        ("error", {"label": "Error", "tone": "error"}),
        (None, None),
    ],
)
def test_card_auth_status(auth, state: str | None, expected: dict | None) -> None:
    row = {"auth": {"state": state}} if state else {}
    got = json.loads(auth.eval(f"JSON.stringify(TS_cardAuthStatus({json.dumps(row)}))"))
    assert got == expected


def test_the_platform_card_prefers_the_sign_in_state() -> None:
    source = PLATFORM.read_text(encoding="utf-8")
    start = source.index("  toolsets: {")
    end = source.index("\n  tools: {", start)
    block = source[start:end]
    assert "window.TS_cardAuthStatus" in block
    assert "row.available === false" in block
    assert block.index("window.TS_cardAuthStatus") < block.index("row.available === false")


def test_a_toolset_without_oauth_keeps_its_available_status() -> None:
    source = PLATFORM.read_text(encoding="utf-8")
    start = source.index("  toolsets: {")
    end = source.index("\n  tools: {", start)
    block = source[start:end]
    assert '{ label: "available", tone: "ok" }' in block
    assert '{ label: "unavailable", tone: "error" }' in block
