"""Platform wave P1b item 1/2/3: model-profiles.jsx card anatomy,
create/edit modal (verb chip, two-column Provider|Model picker with
the discoverable degrade path, Reasoning segment), and the honest
409 delete-guard surfacing.

Static-source checks only (the tests/ui suite convention) - these pin
NEW P1b surface that test_model_profiles_page.py and
test_provider_catalog_profiles.py do not cover (they pin the
architectural "no standalone page" decision and the panel wiring,
not this file's own new anatomy).
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


def _modal_src() -> str:
    src = _src()
    return src[src.index("function MP_ProfileModal("):]


# ---- Item 1: card anatomy -------------------------------------------------


def test_bound_by_and_provider_down_badges_are_data_driven() -> None:
    """SUPERSEDED (platform wave P4): P1b deferred these as P2-dependent
    (the backend didn't serve agent_count/graph_node_count yet); P4
    landed them once it did. Pin that they're derived from real props,
    not static copy invented ahead of the data - see
    test_model_profiles_p4.py for the full badge behavior."""
    card = _card_src()
    assert "MP_BoundBadge" in card
    assert "providerDown" in card


def test_card_fact_rows_are_static_system_behavior_copy() -> None:
    card = _card_src()
    assert "at create · switch · per graph node" in card
    assert "blocked while referenced" in card


def test_card_footer_has_open_and_delete() -> None:
    card = _card_src()
    assert "Open" in card
    assert "Delete" in card


def test_grid_has_an_empty_state() -> None:
    src = _src()
    assert 'data-testid="profiles-empty"' in src


# ---- Item 2: create/edit modal --------------------------------------------


def test_modal_has_a_verb_chip() -> None:
    modal = _modal_src()
    assert 'data-testid="profile-modal-verb-chip"' in modal
    assert 'verb: {isEdit ? "Edit" : "Create"} Model Profile' in modal


def test_modal_provider_column_is_a_row_picker() -> None:
    modal = _modal_src()
    assert 'data-testid="profile-provider-picker"' in modal
    assert 'data-testid={`profile-provider-row-${p.id}`}' in modal
    assert 'data-selected={p.id === providerId ? "true" : "false"}' in modal


def test_modal_model_column_gates_on_discoverable() -> None:
    """Same /_types discoverable gate P1a's provider cards use - not a
    new rule invented for this modal."""
    modal = _modal_src()
    assert "types.data[selectedProvider.provider].discoverable" in modal
    assert "discoverable ?" in modal


def test_modal_model_picker_probes_discovered_models_live() -> None:
    modal = _modal_src()
    assert "/llm_providers/${encodeURIComponent(providerId)}/discovered_models" in modal
    assert "probed live" in modal


def test_modal_model_degrades_to_free_text_when_not_discoverable() -> None:
    """Non-discoverable providers get a free-text model field instead of
    a dead/empty live picker."""
    modal = _modal_src()
    assert 'data-testid="profile-model-freetext"' in modal


def test_modal_reasoning_is_a_segmented_control_not_a_select() -> None:
    modal = _modal_src()
    assert 'data-testid="profile-reasoning-segment"' in modal
    assert "MP_REASONING_LEVELS.map((lvl) =>" in modal
    assert '<span key={lvl} className={"chip"' in modal


def test_modal_footnote_is_present() -> None:
    modal = _modal_src()
    assert 'data-testid="profile-modal-footnote"' in modal
    assert "Deleting is blocked while anything" in modal


# ---- Item 3: honest 409 delete-guard surfacing -----------------------------


def test_delete_guard_surfaces_the_real_error_detail_not_an_invented_shape() -> None:
    """The backend's ReferenceCheck 409s with a plain string in
    `detail` (routers/model_profiles.py via
    api/routers/_references.py's build_reference_block_hook) - never a
    structured {child_kind, count} payload despite that hook's own
    docstring example. Pin that the UI reads err.detail verbatim
    rather than a names list or a parsed JSON shape the wire never
    sends."""
    card = _card_src()
    assert "e.detail" in card
    assert "e.message" in card


def test_delete_confirm_flow_has_a_confirm_and_cancel_step() -> None:
    card = _card_src()
    assert "confirmDelete" in card
    assert "Confirm" in card
    assert "Cancel" in card


def test_bundle_transpiles_with_model_profiles_p1b() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/model-profiles.jsx === */" in text
