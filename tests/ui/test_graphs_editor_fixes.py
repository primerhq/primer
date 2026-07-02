"""Structural regression tests for the graphs-editor bug batch.

Covers five user-reported bugs on the graph editor detail page:

  * #12 — moving a node must not spawn a self-loop edge: edge creation is an
    explicit mode (drag-element vs create-edge gated by ``addEdgeMode``) and
    self-loops are rejected in ``create-edge.onCreate``.
  * #13 — the Static/Conditional toggle wires the new-edge kind and now labels
    itself as such ("new edge:" + tooltips).
  * #14 — the references banner no longer prints the raw ``GET /v1/graphs/{id}
    /status`` line.
  * #15 — Auto-layout re-renders the canvas via a ``layoutNonce`` that feeds the
    canvas topoKey (an x/y-only relayout is otherwise invisible).
  * #16 — the graph description + max_iterations are bound to ``onSetGraph`` and
    included in the PUT save body.

Source-grep + bundle transpile, matching the rest of tests/ui.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
GRAPHS = (UI / "components" / "graphs.jsx").read_text(encoding="utf-8")
CANVAS = (UI / "components" / "graph-canvas.jsx").read_text(encoding="utf-8")


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
    assert "onCreate:" in CANVAS
    assert "edge.source !== edge.target" in CANVAS


def test_connect_handler_still_guards_self_loops() -> None:
    # Belt: the draft-side onConnect also refuses source===target.
    assert "source === target" in CANVAS or "source !== target" in CANVAS


# ---------------------------------------------------------------------------
# #13 — Static/Conditional toggle wires new-edge kind + reads as such
# ---------------------------------------------------------------------------


def test_edge_kind_toggle_wired_to_new_edges() -> None:
    # The toggle state drives the kind of newly created edges in both the
    # click-to-wire and drag-to-connect paths.
    assert "edgeMode" in GRAPHS
    assert 'edgeMode === "static"' in GRAPHS
    assert '"conditional"' in GRAPHS and "json_path" in GRAPHS


def test_edge_kind_toggle_is_labelled_for_clarity() -> None:
    # Clarity fix: the segmented control announces that it applies to the NEXT
    # edge, not existing ones.
    assert "new edge:" in GRAPHS
    assert "Kind for new edges" in GRAPHS
    assert "aria-pressed" in GRAPHS


# ---------------------------------------------------------------------------
# #14 — banner drops the raw API line
# ---------------------------------------------------------------------------


def test_status_banner_has_no_raw_get_line() -> None:
    assert "GET /v1/graphs/{id}/status" not in GRAPHS
    # The human-readable message stays.
    assert "All references resolve" in GRAPHS


# ---------------------------------------------------------------------------
# #15 — Auto-layout actually re-arranges the canvas
# ---------------------------------------------------------------------------


def test_auto_layout_bumps_layout_nonce() -> None:
    assert "onAutoLayout" in GRAPHS
    assert "setLayoutNonce" in GRAPHS
    assert "autoLayout(d)" in GRAPHS


def test_canvas_receives_and_keys_on_layout_nonce() -> None:
    # graphs.jsx passes the nonce; the canvas folds it into topoKey so a bump
    # forces a re-seed from the new positions.
    assert "layoutNonce={layoutNonce}" in GRAPHS
    assert "props.layoutNonce" in CANVAS


# ---------------------------------------------------------------------------
# #16 — description + max_iterations round-trip through save
# ---------------------------------------------------------------------------


def test_graph_description_is_bound_to_set_graph() -> None:
    assert "onSetGraph({ description: v })" in GRAPHS
    assert "onSetGraph={onSetGraph}" in GRAPHS


def test_save_payload_includes_description_and_max_iterations() -> None:
    assert "description: draft.description" in GRAPHS
    assert "max_iterations: draft.max_iterations" in GRAPHS


def test_graph_fields_are_always_reachable_not_gated_on_selection() -> None:
    # #16 real root cause: the description/max_iterations fields used to live
    # ONLY in the no-selection branch of GR_SidePanel, so selecting any node
    # hid them. GR_GraphFields must render OUTSIDE the `selected ? ... :` and
    # BEFORE it, so graph metadata stays editable while a node/edge is
    # selected. The nothing-selected branch now shows read-only GR_GraphStats.
    panel_head = GRAPHS.split("{selected ? (")[0]
    assert "<GR_GraphFields draft={draft} onSetGraph={onSetGraph} />" in panel_head
    assert "function GR_GraphFields(" in GRAPHS
    assert "<GR_GraphStats draft={draft} />" in GRAPHS
    # The old always-hidden block name is gone.
    assert "GR_GraphStatsBlock" not in GRAPHS


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
