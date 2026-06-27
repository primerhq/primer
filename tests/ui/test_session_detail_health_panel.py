"""The fake 'executor missing' pill is replaced by a real graph_status
Health panel + a 'This graph cannot run' banner. A running turn-0 graph
with ok references shows a 'waiting to start' hint, not a blank void."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
DETAIL = (UI / "components" / "session-detail.jsx").read_text(encoding="utf-8")


def test_hardcoded_executor_missing_pill_removed() -> None:
    assert "executor missing" not in DETAIL


def test_health_panel_calls_graph_status() -> None:
    assert "function SD_GraphHealthPanel" in DETAIL
    assert "/status" in DETAIL
    assert "issues" in DETAIL


def test_all_references_resolve_affirmation() -> None:
    assert "all references resolve" in DETAIL.lower()


def test_cannot_run_banner_present() -> None:
    assert "function SD_CannotRunBanner" in DETAIL
    assert "cannot run" in DETAIL.lower()


def test_waiting_to_start_hint() -> None:
    assert "waiting to start" in DETAIL.lower()


def test_bundle_transpiles_with_health() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "SD_GraphHealthPanel" in text
    assert "SD_CannotRunBanner" in text
