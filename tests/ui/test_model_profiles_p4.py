"""Platform wave P4: MP_ProfileCard renders the bound-by/unbound and
provider-down badges the backend P2 wave now actually serves
(agent_count/graph_node_count on the list route), and keeps the
harness-managed delete guard that moved here from provider-catalog.jsx's
old <ul> when the card got wired into PC_ProfilesPanel this same wave.

Static-source checks only (the tests/ui suite convention). Sister of
test_model_profiles_p1b.py (the P1b card anatomy that predates these
badges) and test_provider_catalog_p4.py (the panel-side wiring).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "model-profiles.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _fn_block(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


def _card_src() -> str:
    src = _src()
    return _fn_block(src, "function MP_ProfileCard(", "function MP_ProfilesGrid(")


def _grid_src() -> str:
    src = _src()
    start = src.index("function MP_ProfilesGrid(")
    end = src.index("\n// Create/edit modal", start)
    return src[start:end]


def _bound_badge_src() -> str:
    src = _src()
    start = src.index("function MP_BoundBadge(")
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


# ---- Bound-by / unbound badge ----------------------------------------------


def test_bound_badge_copy_is_bound_by_agents_plus_graph_nodes_when_present() -> None:
    """Exact copy per the P4 brief: 'bound by {agent_count} agents'
    (+ '· {graph_node_count} graph nodes' when >0)."""
    badge = _bound_badge_src()
    assert "`bound by ${agents} agent${agents === 1" in badge
    assert '` · ${nodes} graph node${nodes === 1' in badge
    assert "nodes > 0 ?" in badge


def test_bound_badge_reads_zero_as_unbound_not_missing() -> None:
    """agent_count==0 and graph_node_count==0 is itself the honest,
    meaningful signal (per the backend's own ModelProfileWithUsage
    docstring), not an absent-data case."""
    badge = _bound_badge_src()
    assert '"unbound"' in badge
    assert "const bound = agents > 0 || nodes > 0;" in badge


def test_card_passes_the_real_served_fields_to_the_badge() -> None:
    card = _card_src()
    assert "agentCount={profile.agent_count}" in card
    assert "graphNodeCount={profile.graph_node_count}" in card


def test_grid_threads_provider_down_to_every_card() -> None:
    grid = _grid_src()
    assert "providerDown={providerDown}" in grid


# ---- Provider-down badge ----------------------------------------------------


def test_provider_down_badge_only_renders_when_actually_down() -> None:
    card = _card_src()
    assert "{providerDown ? (" in card
    assert "provider down" in card


# ---- Harness-managed guard (carried over from the pre-P4 <ul>) ------------


def test_harness_managed_profiles_hide_the_delete_button() -> None:
    card = _card_src()
    assert "const harnessManaged = !!profile.harness_id;" in card
    assert 'title="managed by a harness"' in card


def test_delete_button_only_renders_when_not_harness_managed() -> None:
    card = _card_src()
    assert "harnessManaged ? (" in card


def test_bundle_transpiles_with_model_profiles_p4() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/model-profiles.jsx === */" in text
