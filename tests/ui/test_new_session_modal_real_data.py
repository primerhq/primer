"""Regression: no stale "Graph executor is unimplemented" warning in the app or the session detail.

The graph executor at ``primer/graph/executor.py`` is fully implemented; the warning was a stale leftover from a much earlier mock-data scaffold. (This file also pinned the retired
create-session form's data path; that form was removed, see tests/ui/test_retired_new_session_form.py.)
"""

from __future__ import annotations

from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "ui" / "app.jsx"
SDET = (
    Path(__file__).resolve().parents[2]
    / "ui"
    / "components"
    / "session-detail.jsx"
)


def _app() -> str:
    return APP.read_text(encoding="utf-8")


def _sdet() -> str:
    return SDET.read_text(encoding="utf-8")


def test_no_unimplemented_graph_executor_warning_in_app() -> None:
    src = _app()
    assert "Graph executor is unimplemented" not in src, (
        "The graph executor is fully implemented in primer/graph/executor.py; "
        "drop the stale 'unimplemented' warning"
    )


def test_no_unimplemented_graph_executor_banner_in_session_detail() -> None:
    src = _sdet()
    assert "Graph executor is unimplemented" not in src, (
        "session-detail.jsx must not render a 'Graph executor is "
        "unimplemented' Banner — the executor exists"
    )
