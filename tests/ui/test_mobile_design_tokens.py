"""The mobile token block defines --mobile-pad-x, --mobile-pad-y,
--tap-min, --fab-size — used by every later phase's selectors."""

from __future__ import annotations

from pathlib import Path

CSS = Path(__file__).resolve().parents[2] / "ui" / "styles.css"


def test_mobile_pad_x_token() -> None:
    src = CSS.read_text(encoding="utf-8")
    assert "--mobile-pad-x" in src
    assert "16px" in src


def test_mobile_pad_y_token() -> None:
    src = CSS.read_text(encoding="utf-8")
    assert "--mobile-pad-y" in src


def test_tap_min_token() -> None:
    src = CSS.read_text(encoding="utf-8")
    assert "--tap-min" in src
    assert "44px" in src


def test_fab_size_token() -> None:
    src = CSS.read_text(encoding="utf-8")
    assert "--fab-size" in src
    assert "56px" in src
