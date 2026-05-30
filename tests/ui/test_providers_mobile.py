"""providers.jsx (LLM / Embedding / Cross-Encoder list pages) uses
CardList + Fab on mobile, and JSON textareas get an
'Expand to full screen' affordance."""
from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "providers.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_use_viewport() -> None:
    assert "useViewport" in _src()


def test_card_list() -> None:
    assert "CardList" in _src()


def test_fab() -> None:
    src = _src()
    assert "Fab" in src
    assert "New provider" in src


def test_json_expand_handle() -> None:
    src = _src()
    assert "Expand to full screen" in src or "json-expand" in src
