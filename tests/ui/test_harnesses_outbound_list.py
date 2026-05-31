"""Static JSX checks for outbound list — Plan B Phase 7 / Spec B §11.1."""

from __future__ import annotations

from pathlib import Path


HARNESSES = Path(__file__).resolve().parents[2] / "ui" / "components" / "harnesses.jsx"


def _src() -> str:
    return HARNESSES.read_text(encoding="utf-8")


def test_list_has_direction_filter():
    src = _src()
    # Filter chip / control exists with the three labels.
    assert "hr-direction-filter" in src
    assert '"all"' in src or "'all'" in src
    assert '"inbound"' in src or "'inbound'" in src
    assert '"outbound"' in src or "'outbound'" in src
    # When set to a specific direction the URL includes direction=.
    assert "direction=" in src


def test_list_has_build_outbound_button():
    src = _src()
    assert "Build outbound" in src
    # The "Register from git" button label is also adjusted for clarity.
    assert "Register from git" in src


def test_list_outbound_card_branches():
    src = _src()
    # Card rendering branches on direction === "outbound".
    assert 'direction === "outbound"' in src or "direction == 'outbound'" in src
    # Tracked entity count shown
    assert "tracked_entities" in src
    assert "tracked" in src


def test_list_has_push_handler():
    src = _src()
    # Outbound push wires to the /push endpoint.
    assert "/push" in src


def test_list_has_drift_indicator():
    src = _src()
    # The orange dot for outdated outbound.
    assert "hr-drift-dot" in src
    assert "OUTDATED" in src or "outdated" in src
