"""Regression: workers.jsx must wire its filter/status controls, expose
dead-worker cleanup affordances, and drop the fake/inert controls the
FE review flagged (bug #17).

Static-source + bundle-build checks only (matching the rest of the
ui/ suite — no React render).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "workers.jsx"
APP = UI / "app.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


# ---- Filter input is wired (was inert: no value/onChange) ---------------


def test_filter_input_is_stateful() -> None:
    src = _src()
    assert "setFilterText" in src, "filter input must drive state"
    assert 'data-testid="workers-filter"' in src
    assert "onChange={(e) => setFilterText" in src
    assert "value={filterText}" in src


# ---- Status chips are wired (were plain <span>s, no handler) ------------


def test_status_chips_are_wired() -> None:
    src = _src()
    assert "setStatusFilter" in src, "chips must set the status filter"
    assert 'data-testid={`workers-chip-${c}`}' in src
    # The active chip is derived from state, not hardcoded to "all".
    assert 'statusFilter === c ? " active" : ""' in src
    assert 'className="chip active"' not in src, (
        "the 'all' chip must not be hardcoded active"
    )


def test_client_side_filtering_applied() -> None:
    src = _src()
    # The table iterates the filtered list, not the raw worker list.
    assert "filtered.map" in src
    assert "w.status !== statusFilter" in src
    assert "(w.id || \"\").toLowerCase().includes(q)" in src


# ---- Fake "Scheduler: alive · last claim 2s ago" tile removed ----------


def test_fake_scheduler_tile_removed() -> None:
    src = _src()
    assert "last claim 2s ago" not in src, (
        "the hardcoded fake scheduler telemetry tile must be gone"
    )


# ---- Fake per-row "· N sessions" annotation removed --------------------


def test_fake_sessions_annotation_removed() -> None:
    src = _src()
    # The always-empty `sessions` prop and its per-row derivation are gone.
    assert "onWorker" not in src
    assert "s.worker_id === w.id" not in src
    assert "session{onWorker" not in src


def test_app_no_longer_passes_fake_sessions_prop() -> None:
    app = APP.read_text(encoding="utf-8")
    assert "<WorkersPage pushToast={pushToast} />" in app
    assert "<WorkersPage sessions=" not in app


# ---- Dead-worker cleanup affordances (bug #17) -------------------------


def test_per_row_delete_button_on_dead_rows() -> None:
    src = _src()
    assert 'data-testid="worker-delete"' in src
    # Deletes via the new DELETE /workers/{id} endpoint.
    assert '"DELETE"' in src
    assert "/workers/${encodeURIComponent(id)}`" in src
    # Only dead rows get the remove button.
    assert 'w.status === "dead" ? (' in src


def test_bulk_clear_dead_button() -> None:
    src = _src()
    assert 'data-testid="workers-clear-dead"' in src
    assert "/workers/purge_dead" in src
    # Shown only when there are dead workers.
    assert "totals.dead > 0 &&" in src


def test_bulk_clear_dead_confirms_via_modal() -> None:
    src = _src()
    # The bulk button opens a Modal confirm rather than firing inline.
    assert "setClearDeadOpen(true)" in src
    assert "clearDeadOpen && (" in src
    assert "confirmClearDead" in src
    assert "purgeMut.mutate()" in src


# ---- Bundle still transpiles with the rewritten workers.jsx ------------


def test_bundle_transpiles_with_workers() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/workers.jsx === */" in text
