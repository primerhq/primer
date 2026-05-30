"""Run the touch-target audit script against styles.css and assert
every interactive selector in the (max-width: 639px) block declares
or inherits a tap area >= 44x44."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "audit_touch_targets.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "audit_touch_targets", SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_file_exists() -> None:
    assert SCRIPT.exists()


def test_audit_passes_on_current_styles() -> None:
    mod = _load_module()
    failures = mod.audit()
    assert failures == [], (
        f"touch-target audit reported failures: {failures}"
    )


def test_audit_detects_sub_44_button() -> None:
    """Sanity check: feeding a known-bad block fails."""
    mod = _load_module()
    bad_css = """
    @media (max-width: 639px) {
      .nav-item { min-height: 24px; min-width: 24px; }
    }
    """
    failures = mod.audit_text(bad_css)
    assert failures, "audit_text should flag the .nav-item rule"
