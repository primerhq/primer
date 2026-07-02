"""Regression: workers.jsx must open a worker detail view on row click
(bug #18), showing the full membership record over data that already
exists — no invented time-series.

Static-source + bundle-build checks only.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "workers.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_detail_opens_on_row_click() -> None:
    src = _src()
    # The row is clickable and reports its selection to open the drawer.
    assert 'data-testid="worker-row"' in src
    assert "onClick={() => onSelect(w)}" in src
    assert "setDetailId(worker.id)" in src


def test_detail_panel_has_testid() -> None:
    assert 'data-testid="worker-detail"' in _src()


def test_detail_reflects_live_record_not_stale_snapshot() -> None:
    src = _src()
    # Detail is derived from the polled list by id, so it stays fresh.
    assert "workers.find((w) => w.id === detailId)" in src


def test_detail_shows_full_record() -> None:
    src = _src()
    assert "function WorkerDetail(" in src
    # id / host / pid / status / capacity / heartbeat / started all present.
    for label in ("Host / PID", "Status", "Capacity", "Last heartbeat", "Started"):
        assert label in src, f"detail missing {label!r} field"
    # Heartbeat + started shown as relative *and* absolute.
    assert "fmtDate(new Date(w.last_heartbeat))" in src
    assert "relativeTime(startedSecondsAgo)" in src


def test_action_cell_stops_row_click_propagation() -> None:
    src = _src()
    # Drain/Remove buttons must not also open the detail drawer.
    assert "onClick={(e) => e.stopPropagation()}" in src


def test_no_invented_history_chart() -> None:
    src = _src()
    # v1 detail is over existing data only — no fabricated time-series.
    assert "Sparkline" not in src
    assert "per-worker counters or heartbeat history" in src.lower()


def test_bundle_transpiles_with_workers_detail() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "function WorkerDetail(" in text
