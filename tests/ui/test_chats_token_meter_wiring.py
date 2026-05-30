"""Static JSX checks — chats.jsx wires the TokenMeter + compact handler."""

from __future__ import annotations

from pathlib import Path


CHATS_JSX = Path(__file__).resolve().parents[2] / "ui" / "components" / "chats.jsx"


def test_chats_imports_token_meter() -> None:
    src = CHATS_JSX.read_text(encoding="utf-8")
    assert "TokenMeter" in src


def test_chats_handles_usage_envelope() -> None:
    src = CHATS_JSX.read_text(encoding="utf-8")
    assert '"usage"' in src or "'usage'" in src


def test_chats_handles_compaction_envelope() -> None:
    src = CHATS_JSX.read_text(encoding="utf-8")
    assert '"compaction"' in src or "'compaction'" in src


def test_chats_compact_post_handler() -> None:
    src = CHATS_JSX.read_text(encoding="utf-8")
    assert "/compact" in src
