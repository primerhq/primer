"""The graph editor's per-node model-profile override.

An agent's profile is a DEFAULT. A node may name a different one so a
single agent definition runs cheap in one node and reasoning-heavy in
another; the editor has to expose that or the field is unreachable.

Static-source checks, matching the rest of the ui/ suite.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "graphs.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_editor_loads_the_profile_vocabulary() -> None:
    assert '"graphs-editor:model-profiles"' in _src()


def test_the_prop_reaches_the_node_inspector() -> None:
    """Three hops: page -> side panel -> selected-node form. A miss at any
    one renders an empty picker rather than an error."""
    src = _src()
    assert src.count("profilesList") >= 5


def test_the_node_field_writes_profile_id() -> None:
    src = _src()
    assert 'data-testid="gr-node-profile"' in src
    assert "onUpdateNode({ profile_id: e.target.value || null })" in src


def test_the_empty_option_means_the_agent_default_not_no_model() -> None:
    """Clearing the select must fall back to the agent, not unset the
    model; the value is null, and the label says so."""
    src = _src()
    assert "<option value=\"\">use the agent's default</option>" in src


def test_the_backend_accepts_what_the_editor_writes() -> None:
    from primer.model.graph import _AgentNodeRef

    n = _AgentNodeRef.model_validate(
        {"kind": "agent", "id": "n1", "agent_id": "ag", "profile_id": None},
    )
    assert n.profile_id is None
    n2 = _AgentNodeRef.model_validate(
        {"kind": "agent", "id": "n1", "agent_id": "ag", "profile_id": "p--m"},
    )
    assert n2.profile_id == "p--m"


def test_graphs_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    code = b._transform(_src(), "components/graphs.jsx")
    assert code and "GR_SidePanel" in code
