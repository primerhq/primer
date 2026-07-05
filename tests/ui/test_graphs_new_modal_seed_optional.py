"""Static JSX checks: the New-graph modal makes the seed agent OPTIONAL.

Graph creation no longer requires a seed agent — an empty graph can be
created and its runnability is enforced at session-start, not creation.
The Create button is therefore gated only on the in-flight mutation, and
the modal has an empty-graph submit path when no seed agent is chosen.
"""

from __future__ import annotations

from pathlib import Path


GRAPHS = Path(__file__).resolve().parents[2] / "ui" / "components" / "graphs.jsx"


def _src() -> str:
    return GRAPHS.read_text(encoding="utf-8")


def test_create_button_not_gated_on_seed_agent() -> None:
    src = _src()
    # Create is disabled only while the create mutation is running.
    assert "disabled={create.loading}" in src
    # The old seed-agent gate is gone.
    assert "disabled={!seedAgentId || create.loading}" not in src


def test_empty_graph_submit_path_exists() -> None:
    src = _src()
    # When no seed agent is chosen the modal submits an empty graph.
    assert "nodes: []" in src
    assert "edges: []" in src


def test_seed_agent_hint_marks_optional_not_required() -> None:
    src = _src()
    # The seed-agent hint now advertises it as optional.
    assert "optional" in src
    # The old "required" seed-agent hint copy is gone.
    assert "required · the new graph starts with Begin" not in src
