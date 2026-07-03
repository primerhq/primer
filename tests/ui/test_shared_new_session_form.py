"""FD2 — the two near-identical "new session" create forms (app.jsx's
NewSessionModal + studio-sidebar.jsx's NewSessionForm) were unified into ONE
shared component: ui/components/new-session-form.jsx (window.SharedNewSessionForm).

The shared component is the SUPERSET of both: binding-kind toggle, agent/graph
select, optional session `name` (#22), initial instructions, AND a graph's
Begin.input_schema dynamic form (which previously only the app modal had — the
Studio sidebar now inherits it too). These checks pin that wiring so the two
forms can't silently diverge again.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SHARED = UI / "components" / "new-session-form.jsx"
APP = UI / "app.jsx"
SIDEBAR = UI / "components" / "studio-sidebar.jsx"
INDEX = UI / "index.html"


def _shared() -> str:
    return SHARED.read_text(encoding="utf-8")


def test_shared_component_exists_and_is_exported() -> None:
    assert SHARED.exists(), "ui/components/new-session-form.jsx must exist"
    src = _shared()
    assert "function SharedNewSessionForm(" in src
    assert "window.SharedNewSessionForm = SharedNewSessionForm" in src


def test_shared_component_is_used_by_both_call_sites() -> None:
    # app.jsx's NewSessionModal + the Studio sidebar's NewSessionForm both
    # render the ONE shared component instead of their own duplicated fields.
    assert "SharedNewSessionForm" in APP.read_text(encoding="utf-8")
    assert "SharedNewSessionForm" in SIDEBAR.read_text(encoding="utf-8")


def test_shared_component_supports_graph_input_schema() -> None:
    src = _shared()
    # Reads the selected graph's Begin.input_schema and packages the answers
    # into `graph_input` on submit; falls back to the instructions textarea.
    assert "input_schema" in src
    assert "begin" in src.lower()
    assert "graph_input" in src
    assert "initial_instructions" in src


def test_shared_component_supports_optional_name() -> None:
    src = _shared()
    assert 'data-testid="new-session-name"' in src
    # Only sent when non-empty (#22).
    assert "body.name" in src


def test_shared_component_carries_both_testids() -> None:
    src = _shared()
    # The inline (Studio) overlay + the name field testids moved here (FD2).
    assert 'data-testid="new-session-form"' in src
    assert 'data-testid="new-session-name"' in src


def test_index_loads_shared_form_before_both_sites() -> None:
    lines = INDEX.read_text(encoding="utf-8").splitlines()
    order = [ln for ln in lines if "text/babel" in ln]
    order_str = "\n".join(order)
    assert "components/new-session-form.jsx" in order_str, (
        "new-session-form.jsx must be registered in index.html"
    )

    def idx(needle: str) -> int:
        return next(i for i, ln in enumerate(order) if needle in ln)

    shared_i = idx("components/new-session-form.jsx")
    assert shared_i < idx("components/studio-sidebar.jsx")
    assert shared_i < idx("app.jsx")
    # Loaded after shared.jsx (which defines Modal/Btn/Icon it consumes).
    assert idx("components/shared.jsx") < shared_i


def test_bundle_transpiles_with_shared_new_session_form() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    assert "/* === components/new-session-form.jsx === */" in body.decode("utf-8")
