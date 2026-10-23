"""Providers IA restructure: form tests and platform mount pins.

Retargeted per plan 02 Task 20: the 14 tests pinning the old chip row,
register dropdowns, merged grid and catalog modal were retired;
one PC_RegisterOverlay mount serves create and edit; the form tests and
platform page inline mount remain.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"


def _catalog() -> str:
    return (UI / "components" / "provider-catalog.jsx").read_text(encoding="utf-8")


def _form() -> str:
    return (UI / "components" / "provider-form.jsx").read_text(encoding="utf-8")


def _platform() -> str:
    return (UI / "components" / "console" / "nv-platform.jsx").read_text(encoding="utf-8")


def test_one_form_modal_serves_both_create_and_edit() -> None:
    src = _catalog()
    assert "<PC_RegisterOverlay" in src
    assert src.count("<PC_RegisterOverlay") == 1


# ---------------------------------------------------------------------------
# Secrets never round-trip the mask; id is locked
# ---------------------------------------------------------------------------


def test_id_field_is_disabled_while_editing() -> None:
    """01a063ab: the in-form kind <select> is gone entirely (kind always
    arrives preselected from the Register dropdown), so only the id/Name
    PC_Field is left to gate - exactly one `disabled={editing}` JSX prop
    now exists (a second, unrelated `disabled` guards Save on `busy`, not
    `editing`, so this counts the literal prop, not the word)."""
    src = _form()
    assert src.count("disabled={editing}") == 1
    id_block = src[src.index('key: "id", label: "Name"'):]
    id_block = id_block[:id_block.index("/>")]
    assert "disabled={editing}" in id_block


def test_secret_fields_are_not_stripped_on_edit_mount() -> None:
    """01a05198: no frontend strip-on-edit mechanism any more - the
    backend now recognizes the served mask on write (preserve_masked_
    secrets, primer/model/common.py) and restores the real credential,
    so the form simply leaves a secret field's draft value as whatever
    GET served (the mask string), same as every other field."""
    src = _form()
    assert "strippedRef" not in src
    assert "secretMasks" not in src
    assert "setSecretMasks" not in src


def test_secret_field_has_no_special_value_override() -> None:
    """PC_Field renders every field's value uniformly now - no
    secret-specific placeholder/mask machinery left to diverge from the
    plain `value || ""` every other field uses."""
    src = _form()
    field = src[src.index("function PC_Field"):src.index("function PC_ProbePanel")]
    assert "secretMask" not in field
    assert 'value={value || ""}' in field


def test_mask_resubmitted_unchanged_enables_save() -> None:
    """NEW CONTRACT (01a05198): missingRequired() reverted to schema-
    required only - a secret field's draft value on edit is the served
    mask string (non-blank), so a schema-required secret left untouched
    no longer reads as "missing" and Save is not withheld. The backend's
    preserve_masked_secrets hook is what makes resubmitting that string
    safe (it restores the real value rather than persisting the mask
    literally) - this test pins the FRONTEND half of that contract: nothing
    here forces a re-type any more."""
    src = _form()
    missing_required = src[src.index("const missingRequired = () =>"):]
    missing_required = missing_required[:missing_required.index("const modelRowsIncomplete")]
    assert "hadSecret" not in missing_required
    assert "secretMasks" not in missing_required
    assert "if (!norm.required) return false;" in missing_required
    assert "blank(holder[norm.key])" in missing_required


def test_a_cleared_optional_secret_still_gates_correctly() -> None:
    """A secret field is not special-cased for blank-checking either
    direction: an OPTIONAL secret the operator explicitly clears (now
    genuinely blank, not the mask) still passes missingRequired() (matches
    a fresh, never-set optional field's own behavior) - the backend's
    preserve_masked_secrets only restores a value that MATCHES the served
    mask pattern (primer/model/common.py's _matches_served_mask); a
    genuinely empty submission does not match it and is stored as
    cleared, not restored. A REQUIRED secret cleared the same way is
    still caught by the unconditional `blank(holder[norm.key])` check
    below - no separate branch needed for either case."""
    src = _form()
    missing_required = src[src.index("const missingRequired = () =>"):]
    missing_required = missing_required[:missing_required.index("const modelRowsIncomplete")]
    # One unconditional required-check covers both create and edit, for
    # every field regardless of secret-ness - the exact simplicity the
    # interim hadSecret gate had to give up safety for.
    assert missing_required.count("blank(") == 2  # draft.id + holder[norm.key]


def test_fields_and_probe_panel_are_a_real_css_grid() -> None:
    """01a063ab: the right column now also hosts PC_InvalidateAction for
    classes with no probe panel (e.g. cross_encoder), so the grid gates
    on showRightColumn (showProbePanel || showInvalidate), not
    showProbePanel alone."""
    src = _form()
    assert 'display: "grid"' in src
    assert 'gridTemplateColumns: showRightColumn ? "minmax(0, 1fr) 240px" : "minmax(0, 1fr)"' in src
    # The old flex tug-of-war (flex: 1 fields vs. a fixed-width sibling
    # in the same flex row) must be gone, not just supplemented.
    assert 'className="row" style={{ gap: 20, alignItems: "flex-start" }}' not in src


# ---------------------------------------------------------------------------
# Kill the three-layer stack: the platform page mounts the catalog inline
# ---------------------------------------------------------------------------


def test_platform_page_mounts_the_catalog_inline_not_via_overlay() -> None:
    src = _platform()
    # The page hands the catalog the view's provider type ("all" without one): view=platform:providers:<type>, Platform revamp Plan 01 Task 6 (seam I-1).
    assert 'if (nav === "providers") return <NV_ProvidersPlatPage type={con.view.sub || "all"} />;' in src
    inline = src[src.index("function NV_ProvidersPlatPage"):]
    inline = inline[:inline.index("\nfunction NV_PlatPage")]
    assert "<window.ProviderCatalog" in inline
    assert "openOverlay" not in inline


def test_platform_page_no_longer_carries_its_own_provider_duplicate() -> None:
    """The old hand-rolled family-pill grid (a second, redundant copy of
    what ProviderCatalog already does) must be gone, not left running
    alongside the inline mount."""
    src = _platform()
    assert "isProviders" not in src
    assert "NV_PROV_CLASSES" not in src


def test_bundle_transpiles_with_the_ia_restructure() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/provider-catalog.jsx === */" in text
    assert "/* === components/provider-form.jsx === */" in text
    assert "/* === components/console/nv-platform.jsx === */" in text
