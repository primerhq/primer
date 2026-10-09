"""Structural regression tests for the graph canvas the editor draws on (graph-canvas.jsx).

The bug batch these came from was reported against the old editor; the canvas it fixed is the one the graph builder still wraps (``GB_Canvas`` -> ``GR_Canvas``), so the canvas pins stay:

  * #12 - moving a node must not spawn a self-loop edge: edge creation is an explicit mode (drag-element vs create-edge gated by ``addEdgeMode``) and self-loops are rejected in
    ``create-edge.onCreate``.
  * #14 - the references banner of the graph status panel prints no raw ``GET /v1/graphs/{id}/status`` line (``GR_GraphStatusPanel`` is kept).
  * #15 - Auto-layout re-renders the canvas via a ``layoutNonce`` that feeds the canvas topoKey (an x/y-only relayout is otherwise invisible).

The pins on the old editor's own markup (#13 the Static/Conditional toggle, #14 the references banner, #15 the Auto-layout button, #16 the description and max_iterations fields) went with it;
what the builder has instead is pinned in ``test_graph_builder_parity.py``.

Source-grep + bundle transpile, matching the rest of tests/ui.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CANVAS = (UI / "components" / "graph-canvas.jsx").read_text(encoding="utf-8")
GRAPHS = (UI / "components" / "graphs.jsx").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# #12 — explicit edge-creation mode; node moves can't create (self-loop) edges
# ---------------------------------------------------------------------------


def test_edge_create_is_an_explicit_gated_mode() -> None:
    # drag-element (move) is on only OUTSIDE addEdgeMode; create-edge (connect)
    # is on only INSIDE addEdgeMode. Both are gated by G6's `enable` callback so
    # a node-move drag can never trigger create-edge.
    assert "drag-element" in CANVAS
    assert "create-edge" in CANVAS
    assert "enable: () => !cb.current.addEdgeMode" in CANVAS
    assert "enable: () => !!cb.current.addEdgeMode" in CANVAS


def test_create_edge_rejects_self_loops() -> None:
    # onCreate returns false for source===target; G6 only commits an edge when
    # onCreate returns truthy, so an accidental self-drop creates nothing.
    # (The guard moved from a ternary to an early return when the graph-builder
    # revamp added the illegal-target rejections below; either form is fine.)
    assert "onCreate:" in CANVAS
    assert "edge.source !== edge.target" in CANVAS or "edge.source === edge.target" in CANVAS


def test_create_edge_rejects_targets_the_model_forbids() -> None:
    # ui/graph-builder/WIRING.md §6.3 - a fan-out feeds its copies through its
    # own specs, so an outgoing edge from one is a persist-time violation; the
    # start step takes no incoming edge and a finish step leads nowhere. Refuse
    # all three at the gesture with an explanation instead of on save.
    assert 'src.kind === "fan_out"' in CANVAS
    assert 'dst.kind === "begin"' in CANVAS
    assert 'src.kind === "end"' in CANVAS
    assert "onIllegalEdge" in CANVAS


def test_connect_handler_still_guards_self_loops() -> None:
    # Belt: the draft-side onConnect also refuses source===target.
    assert "source === target" in CANVAS or "source !== target" in CANVAS


# ---------------------------------------------------------------------------
# #15 - Auto-layout actually re-arranges the canvas
# ---------------------------------------------------------------------------


def test_canvas_receives_and_keys_on_layout_nonce() -> None:
    # the builder passes the nonce (pinned in test_graph_builder_parity.py); the
    # canvas folds it into topoKey so a bump forces a re-seed from the new positions.
    assert "props.layoutNonce" in CANVAS


# ---------------------------------------------------------------------------
# #14 - the references banner (GR_GraphStatusPanel, which is kept and still renders above the builder)
# ---------------------------------------------------------------------------


def test_status_banner_has_no_raw_get_line() -> None:
    assert "GET /v1/graphs/{id}/status" not in GRAPHS
    # The human-readable message stays.
    assert "All references resolve" in GRAPHS


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
