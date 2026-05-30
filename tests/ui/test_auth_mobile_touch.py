"""Login + register forms apply .touch-target to their buttons and
use font-size: 16px on inputs (via CSS, not inline) — the global
mobile media block in styles.css enforces 16px, so the static check
here verifies the button class additions only."""
from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "auth.jsx"


def test_login_submit_uses_touch_target_class() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "touch-target" in src


def test_no_inline_small_button_sizes() -> None:
    src = SRC.read_text(encoding="utf-8")
    for bad in ("height: 28px", "height: 30px", "height: 32px"):
        assert bad not in src, f"auth screen has a sub-44px button height: {bad}"
