"""Static JSX checks — TokenMeter renders with correct color band."""

from __future__ import annotations

from pathlib import Path


# Adapt path to wherever you placed the component.
CANDIDATE_PATHS = [
    Path(__file__).resolve().parents[2] / "ui" / "components" / "shared" / "token-meter.jsx",
    Path(__file__).resolve().parents[2] / "ui" / "components" / "token-meter.jsx",
]


def _component_path() -> Path:
    for p in CANDIDATE_PATHS:
        if p.exists():
            return p
    raise FileNotFoundError(f"token-meter.jsx not found in any of {CANDIDATE_PATHS}")


def test_component_file_exists() -> None:
    _component_path()  # raises if missing


def test_color_band_logic_present() -> None:
    src = _component_path().read_text(encoding="utf-8")
    assert "0.6" in src or "60" in src
    assert "0.9" in src or "90" in src
    assert "green" in src.lower()
    assert "amber" in src.lower() or "yellow" in src.lower()
    assert "red" in src.lower()


def test_exports_to_window() -> None:
    src = _component_path().read_text(encoding="utf-8")
    assert "TokenMeter" in src
    assert "window.TokenMeter" in src


def test_band_backgrounds_use_design_tokens_not_magic_hex() -> None:
    # FC3: the green/amber/red band backgrounds must be driven by the
    # semantic design tokens, not the old hardcoded #hex literals.
    src = _component_path().read_text(encoding="utf-8")
    for stale in ("#1f7a3a", "#b8860b", "#c0392b"):
        assert stale not in src, f"token-meter.jsx must not hardcode {stale}; use var(--*)"
    assert "var(--green)" in src
    assert "var(--amber)" in src
    assert "var(--red)" in src
