"""FD2 -- the two near-identical "new session" create forms were unified
into ONE shared component: ui/components/new-session-form.jsx
(window.SharedNewSessionForm).

The shared component is the SUPERSET of both: binding-kind toggle,
agent/graph select, optional session `name` (#22), initial instructions,
AND a graph's Begin.input_schema dynamic form. These checks pin that
wiring so a second create form cannot quietly grow beside it.

Its consumer is now the shell's `new-session` overlay, which covers the
case lazy creation cannot: a session bound to an agent or graph the
operator picks rather than the workspace default.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SHARED = UI / "components" / "new-session-form.jsx"
APP = UI / "app.jsx"
INDEX = UI / "index.html"


def _shared() -> str:
    return SHARED.read_text(encoding="utf-8")


def test_shared_component_exists_and_is_exported() -> None:
    assert SHARED.exists(), "ui/components/new-session-form.jsx must exist"
    src = _shared()
    assert "function SharedNewSessionForm(" in src
    assert "window.SharedNewSessionForm = SharedNewSessionForm" in src


def test_every_create_surface_renders_the_shared_component() -> None:
    """The create surface must not duplicate the tricky field logic.
    The console's designer panel (flag day) re-implements the CHROME but
    reuses the ONE schema-driven field component and keeps the submit
    contract, which its own test file pins line by line."""
    host = UI / "components" / "console" / "nv-overlays.jsx"
    src = host.read_text(encoding="utf-8")
    assert "SharedNewSessionSchemaField" in src
    assert "graph_input" in src and "initial_instructions" in src


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
    assert shared_i < idx("components/console/nv-overlays.jsx")
    assert shared_i < idx("app.jsx")
    # Loaded after shared.jsx (which defines Modal/Btn/Icon it consumes).
    assert idx("components/shared.jsx") < shared_i


def test_bundle_transpiles_with_shared_new_session_form() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    assert "/* === components/new-session-form.jsx === */" in body.decode("utf-8")


def test_agent_binding_may_be_left_unpicked() -> None:
    """Submitting with no agent selected means "the system default".

    The gate used to require a pick, which made a default agent
    unreachable from the console even once one was configured.
    """
    src = _shared()
    assert 'kind === "agent" ? true : !!graphId' in src, (
        "the agent branch of canSubmit must not require an agentId"
    )


def test_unpicked_agent_omits_binding_rather_than_sending_null() -> None:
    """{kind:"agent", agent_id:null} would ask to bind to an agent
    named null; omitting the key is what requests the default."""
    src = _shared()
    assert "if (binding) body.binding = binding;" in src

