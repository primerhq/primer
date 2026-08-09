"""Static checks for the native Studio2 session document (plan tasks 8-9)."""

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_session_kind_registered_natively() -> None:
    src = _read("components/studio2/s2-doc-session.jsx")
    assert 'registerKind("session"' in src


def test_reuses_classic_panels_not_a_fork() -> None:
    src = _read("components/studio2/s2-doc-session.jsx")
    assert "window.SessionAgentPanel" in src
    assert "window.SessionGraphPanel" in src
    # No transcript reimplementation in studio2.
    assert "SA_useSessionConversation" not in src


def test_graph_sessions_pick_the_graph_panel() -> None:
    src = _read("components/studio2/s2-doc-session.jsx")
    assert "graph_id" in src and "isGraph" in src


def test_feeds_workspace_context() -> None:
    src = _read("components/studio2/s2-doc-session.jsx")
    assert "noteActiveDoc(" in src


def test_error_state_states_cause() -> None:
    # Design-pack rule 14: inline errors state cause, not just "error".
    src = _read("components/studio2/s2-doc-session.jsx")
    assert "deleted" in src


def test_loads_after_legacy_so_native_wins() -> None:
    html = _read("index.html")
    assert html.index("s2-legacy.jsx") < html.index("s2-doc-session.jsx")
