"""Platform wave P1b item 1/2/3: model-profiles.jsx card anatomy,
create/edit modal (verb chip, two-column Provider|Model picker with
the discoverable degrade path, Reasoning segment), and the honest
409 delete-guard surfacing. Platform revamp Plan 03 retired the card
(the LLM Profiles page draws its cards through the kit) and turned the
modal into the profile overlays; what is left here pins the overlay's
source.

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


def _modal_src() -> str:
    src = _src()
    return src[src.index("function MP_ProfileModal("):]


# RETIRED (Plan 03, Task 6): the card anatomy tests (test_bound_by_and_provider_down_badges_are_data_driven,
# test_card_fact_rows_are_static_system_behavior_copy, test_card_footer_has_open_and_delete, test_grid_has_an_empty_state). MP_ProfileCard and
# MP_ProfilesGrid were mounted only by the Providers page's profile panels, which left with Plan 02 (#804); the LLM Profiles page draws its cards
# through the platform kit. The bound-by copy is pinned by test_a_single_card_shows_imported_provider_model_window_and_usage and
# test_usage_says_unbound_at_zero (tests/ui/test_llm_profiles_page.py), the open and delete by the page's card (pk-card-open, the menu's
# nv-plat-delete: tests/ui/test_platform_list_page.py), and its empty state by tests/ui/test_platform_empty_states.py. The provider-down badge is
# not carried over: the provider's own card on Platform > Providers shows its reachability.


# ---- Item 2: create/edit modal --------------------------------------------


def test_modal_has_no_verb_chip() -> None:
    """The palette verb was a designer's annotation that shipped as UI (lead sweep L3)."""
    modal = _modal_src()
    assert "verb-chip" not in modal and "verb:" not in modal


def test_modal_provider_is_a_select_of_the_llm_providers() -> None:
    """Platform revamp Plan 03, Task 3: the two-column row picker is a PK_Field select. The options are the LLM providers by id and type (MP_providerOptions), and a
    provider that is not listed stays an option, so an edit never changes it by showing something else."""
    modal = _modal_src()
    assert 'data-testid="mp-single-provider"' in modal
    assert "MP_providerOptions(" in modal
    assert "profile-provider-picker" not in modal and "profile-provider-row-" not in modal


# RETIRED (Plan 03, Task 3): test_modal_model_column_gates_on_discoverable, test_modal_model_picker_probes_discovered_models_live and
# test_modal_model_degrades_to_free_text_when_not_discoverable. Creating profiles from a provider's discovered models moved to the Register overlay's import (design 7.1
# and 13.3); the single-profile overlay edits the model name as text, and what that edit sends is pinned by test_a_single_edit_keeps_every_field_the_form_does_not_show
# (tests/ui/test_llm_profile_overlays.py).


def test_the_overlay_offers_no_reasoning_control_until_plan_03_task_7() -> None:
    """Replaces test_modal_reasoning_is_a_segmented_control_not_a_select. Model calls apply a profile's ``config`` (reasoning, extended) now; the overlay does not draw a control
    for it yet. The Reasoning control returns in plan 03 Task 7, which flips this pin, and until then ``config`` is kept as stored: the overlay sends it back whole (MP_editBody)."""
    modal = _modal_src()
    assert "profile-reasoning-segment" not in modal
    assert "MP_REASONING_LEVELS.map(" not in modal


def test_modal_footnote_is_present() -> None:
    modal = _modal_src()
    assert 'data-testid="profile-modal-footnote"' in modal
    assert "Deleting is refused while anything uses it." in modal


# ---- Item 3: the delete guard ----------------------------------------------
# RETIRED (Plan 03, Task 6): test_delete_guard_surfaces_the_real_error_detail_not_an_invented_shape and
# test_delete_confirm_flow_has_a_confirm_and_cancel_step pinned MP_ProfileCard's own confirm and its reading of the 409. A profile is deleted from
# the LLM Profiles page's card menu now, through NV_deleteRow: its confirm and the refusal said in the words of window.primerApi.readRefusal are
# pinned by tests/ui/test_platform_delete_confirm.py, and a harness-managed profile has no Delete at all
# (test_a_managed_profile_has_no_delete_and_says_who_manages_it).


def test_bundle_transpiles_with_model_profiles_p1b() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/model-profiles.jsx === */" in text
