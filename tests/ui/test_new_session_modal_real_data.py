"""Regression: NewSessionModal must fetch real agents/graphs/workspaces
from the API and submit via the POST /workspaces/{ws}/sessions endpoint.

Bug it locks in: pre-fix, the modal read from `window.MOCK.AGENTS`,
`window.MOCK.GRAPHS`, `window.MOCK.WORKSPACES` and carried a hardcoded
"Graph executor is unimplemented" warning. The graph executor at
`primer/graph/executor.py` is fully implemented — the warning was a
stale leftover from a much earlier mock-data scaffold.
"""

from __future__ import annotations

from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "ui" / "app.jsx"
SHARED = (
    Path(__file__).resolve().parents[2]
    / "ui"
    / "components"
    / "new-session-form.jsx"
)
SDET = (
    Path(__file__).resolve().parents[2]
    / "ui"
    / "components"
    / "session-detail.jsx"
)


def _app() -> str:
    return APP.read_text(encoding="utf-8")


def _shared_body() -> str:
    # The create-session data path (fetch + POST + mutation) was unified into
    # window.SharedNewSessionForm (FD2); app.jsx's NewSessionModal is now a
    # thin wrapper that renders it.
    src = SHARED.read_text(encoding="utf-8")
    start = src.index("function SharedNewSessionForm")
    end = src.index("window.SharedNewSessionForm =", start)
    return src[start:end]


def _sdet() -> str:
    return SDET.read_text(encoding="utf-8")


def test_modal_does_not_read_mock_data() -> None:
    body = _shared_body()
    assert "window.MOCK" not in body, (
        "The new-session form must not read from window.MOCK — fetch real "
        "agents/graphs/workspaces from the API instead"
    )


def test_modal_fetches_real_endpoints() -> None:
    body = _shared_body()
    for url in ("/agents?limit=200", "/graphs?limit=200", "/workspaces?limit=200"):
        assert url in body, f"The new-session form must fetch {url}"


def test_modal_posts_to_workspace_sessions_endpoint() -> None:
    body = _shared_body()
    assert "/workspaces/" in body and "/sessions" in body, (
        "The new-session form must POST to /workspaces/{ws}/sessions"
    )
    assert '"POST"' in body or "'POST'" in body, (
        "The new-session form must issue a POST mutation"
    )


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


def test_modal_uses_use_resource_and_use_mutation() -> None:
    body = _shared_body()
    assert "useResource" in body, "form should consume useResource"
    assert "useMutation" in body, "form should consume useMutation"
