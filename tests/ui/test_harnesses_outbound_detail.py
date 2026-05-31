"""Static JSX checks for outbound detail page — Plan B Phase 9 / Spec B §11.3."""

from __future__ import annotations

from pathlib import Path


HARNESSES = Path(__file__).resolve().parents[2] / "ui" / "components" / "harnesses.jsx"
BUILDER = Path(__file__).resolve().parents[2] / "ui" / "components" / "harness_outbound_builder.jsx"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_detail_renders_tracked_entities():
    src = _src(HARNESSES)
    assert "tracked_entities" in src
    assert "HR_TrackedEntitiesPanel" in src


def test_detail_has_check_drift_and_push_buttons():
    src = _src(HARNESSES)
    assert "Check drift" in src
    assert "Push" in src
    assert "/build" in src
    assert "/push" in src


def test_detail_shows_last_pushed_commit():
    src = _src(HARNESSES)
    assert "last_pushed_commit" in src
    # Short SHA + timestamp + bundle hash
    assert "last_pushed_at" in src
    assert "last_pushed_bundle_hash" in src
    # Renders via the dedicated panel
    assert "HR_LastPushedPanel" in src


def test_detail_direction_aware():
    src = _src(HARNESSES)
    # Branches on direction === "outbound"
    assert 'direction === "outbound"' in src or "direction == 'outbound'" in src
    # Outbound replaces inbound action buttons (Fetch/Sync gone for outbound rows).
    assert "isOutbound" in src


def test_detail_has_last_operation_error_box():
    src = _src(HARNESSES)
    assert "hr-last-operation-error" in src
    assert "last_operation_error" in src
    # Structured-error rendering (code + message)
    assert ".code" in src
    assert ".message" in src


def test_detail_edit_tracked_opens_wizard_at_step_3():
    src = _src(HARNESSES)
    assert "Edit tracked entities" in src
    # The detail page reuses HarnessOutboundBuilder with initialStep=3.
    assert "initialStep" in src
    assert "initialHarness" in src


def test_builder_supports_edit_via_put_tracked_entities():
    src = _src(BUILDER)
    # When initialHarness is supplied, the edit path PUTs /tracked_entities.
    assert "/tracked_entities" in src
    assert '"PUT"' in src or "'PUT'" in src
