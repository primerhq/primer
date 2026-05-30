"""Static check: CardList + Card defined, props match the contract."""
from __future__ import annotations
from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[2]
    / "ui" / "components" / "shared" / "card-list.jsx"
)

def test_file_exists() -> None:
    assert SRC.exists()

def test_card_list_defined() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "function CardList" in src or "const CardList" in src

def test_card_defined() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "function Card" in src or "const Card" in src

def test_items_and_render_card_props() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "items" in src
    assert "renderCard" in src

def test_card_props_title_subtitle_pill_meta_onclick() -> None:
    src = SRC.read_text(encoding="utf-8")
    for prop in ("title", "subtitle", "pill", "meta", "onClick"):
        assert prop in src, f"missing {prop} prop"

def test_exported_to_window() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "window.CardList" in src
    assert "window.Card" in src
