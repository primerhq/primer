"""Platform wave P4: provider-catalog.jsx consumes what the P2/P3
backend waves built - reachable/unreachable badges + last-probe/error
fact rows on PC_InstanceCard (virgin rows badge-less), and
PC_ProfilesPanel wired to real MP_ProfileCard/MP_ProfilesGrid cards
with a fixed MP_ProfileModal invocation and a provider-down join.

Static-source checks only (the tests/ui suite convention). Sister of
test_provider_catalog_profiles.py (profile-panel behavior, retargeted
onto model-profiles.jsx this same wave) and test_model_profiles_p4.py
(the card-side half of the badge wiring).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "provider-catalog.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _card_src() -> str:
    src = _src()
    start = src.index("function PC_InstanceCard(")
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


def _panel_src() -> str:
    src = _src()
    start = src.index("function PC_ProfilesPanel(")
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


# ---- Reachability badge + fact rows ---------------------------------------


def test_virgin_rows_render_no_reachability_badge() -> None:
    card = _card_src()
    assert "const virgin = row.last_probe_at == null;" in card
    # PC_ReachabilityBadge itself lives above PC_InstanceCard; check its
    # own early-return there instead.
    src = _src()
    badge_fn_start = src.index("function PC_ReachabilityBadge(")
    badge_fn_end = src.index("\n}", badge_fn_start)
    badge_fn = src[badge_fn_start:badge_fn_end]
    assert "if (lastProbeAt == null) return null;" in badge_fn


def test_reachable_and_unreachable_are_derived_from_last_probe_ok() -> None:
    src = _src()
    badge_fn_start = src.index("function PC_ReachabilityBadge(")
    badge_fn_end = src.index("\n}", badge_fn_start)
    badge_fn = src[badge_fn_start:badge_fn_end]
    assert '"reachable" : "unreachable"' in badge_fn


def test_card_shows_last_probed_and_error_fact_rows() -> None:
    card = _card_src()
    assert "last probed" in card
    assert "row.last_error" in card
    assert 'data-testid={`provider-card-probe-error-${row.id}`}' in card


def test_error_row_only_renders_when_actually_unreachable() -> None:
    card = _card_src()
    assert "unreachable && row.last_error" in card


# ---- PC_ProfilesPanel: real card wiring + the fixed modal invocation ------


def test_panel_renders_the_real_profiles_grid_not_a_bare_list() -> None:
    panel = _panel_src()
    assert "<window.MP_ProfilesGrid" in panel
    assert "<ul" not in panel


def test_modal_invocation_passes_open_and_the_full_provider_list() -> None:
    """Fixes a live bug: the pre-P4 call site passed a `providerId` prop
    MP_ProfileModal does not accept, and no `open` prop at all, so
    `if (!open) return null;` inside it fired unconditionally - "New
    profile" always rendered nothing."""
    panel = _panel_src()
    assert "<window.MP_ProfileModal" in panel
    assert "providers={providerRows}" in panel
    modal_start = panel.index("<window.MP_ProfileModal")
    modal_end = panel.index("/>", modal_start)
    modal_call = panel[modal_start:modal_end]
    assert " providerId={providerId}" not in modal_call
    assert "open" in modal_call


def test_modal_prefills_the_current_provider_on_create_only() -> None:
    panel = _panel_src()
    assert "prefill={editing ? null : { provider_id: providerId }}" in panel


def test_provider_down_is_joined_from_the_already_fetched_provider_list() -> None:
    """model_profiles' own list route has no provider-down join
    (_enrich_with_usage only tallies agent_count/graph_node_count) - the
    frontend cross-references this provider's own row instead, and
    treats a never-probed provider as NOT down (same virgin rule the
    reachability badge uses)."""
    panel = _panel_src()
    assert "thisProvider.last_probe_at != null" in panel
    assert "!thisProvider.last_probe_ok" in panel


def test_bundle_transpiles_with_provider_catalog_p4() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/provider-catalog.jsx === */" in text
