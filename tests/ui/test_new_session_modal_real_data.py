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


def test_modal_does_not_read_mock_data() -> None:
    src = _app()
    start = src.index("function NewSessionModal")
    end = src.index("ReactDOM.createRoot", start)
    body = src[start:end]
    assert "window.MOCK" not in body, (
        "NewSessionModal must not read from window.MOCK — fetch real "
        "agents/graphs/workspaces from the API instead"
    )


def test_modal_fetches_real_endpoints() -> None:
    src = _app()
    start = src.index("function NewSessionModal")
    end = src.index("ReactDOM.createRoot", start)
    body = src[start:end]
    for url in ("/agents?limit=200", "/graphs?limit=200", "/workspaces?limit=200"):
        assert url in body, f"NewSessionModal must fetch {url}"


def test_modal_posts_to_workspace_sessions_endpoint() -> None:
    src = _app()
    start = src.index("function NewSessionModal")
    end = src.index("ReactDOM.createRoot", start)
    body = src[start:end]
    assert "/workspaces/" in body and "/sessions" in body, (
        "NewSessionModal must POST to /workspaces/{ws}/sessions"
    )
    assert '"POST"' in body or "'POST'" in body, (
        "NewSessionModal must issue a POST mutation"
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
    src = _app()
    start = src.index("function NewSessionModal")
    end = src.index("ReactDOM.createRoot", start)
    body = src[start:end]
    assert "useResource" in body, "modal should consume useResource"
    assert "useMutation" in body, "modal should consume useMutation"
