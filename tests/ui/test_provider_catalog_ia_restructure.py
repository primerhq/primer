"""Providers IA restructure (01a04d6a, user directive supersedes the
mockup where they conflict): ONE page, all types together with a type
filter, Register-by-type, card-click opens the SAME overlay in edit
mode, form spacing fixed. Static source checks, same technique as
tests/ui/test_provider_catalog.py.
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


# ---------------------------------------------------------------------------
# ONE page, all types together, type filter defaulting to All
# ---------------------------------------------------------------------------


def test_all_is_the_default_class_and_the_first_type_chip() -> None:
    src = _catalog()
    assert 'React.useState(initialClass || "all")' in src, (
        "the catalog must default to the merged All view, not the first "
        "real class"
    )
    assert 'const PC_ALL_TYPE_CHIPS = [{ key: "all", label: "All" }, ...PROVIDER_CLASSES];' in src


def test_all_instances_grid_merges_only_crud_classes() -> None:
    """Panel classes (vector stores, workspaces, channels) keep their own
    purpose-built list/detail pair - a generic card would either omit or
    fake their class-specific actions (reindex, channel rules, ...)."""
    src = _catalog()
    grid = src[src.index("function PC_AllInstancesGrid"):]
    grid = grid[:grid.index("\nfunction ", 1)]
    assert 'crudClasses = PROVIDER_CLASSES.filter((c) => c.form === "crud")' in grid
    assert "Promise.all(crudClasses.map(" in grid
    assert 'data-testid={`provider-card-type-${row.id}`}' in grid, (
        "every merged card must carry its own type label"
    )


def test_type_filter_is_a_plain_presentational_component() -> None:
    """Renamed from PC_FamilyChips - same chip-row markup, just fed the
    synthetic All entry by its caller rather than opining about it."""
    src = _catalog()
    chips = src[src.index("function PC_TypeFilter"):]
    chips = chips[:chips.index("\nfunction ", 1)]
    assert 'data-testid="provider-chips"' in chips
    assert "classes.map((cls) =>" in chips


# ---------------------------------------------------------------------------
# Register provider names the TYPE, independent of the active filter
# ---------------------------------------------------------------------------


def test_register_all_lists_every_type_with_no_fetch() -> None:
    """Unlike PC_RegisterDropdown (kind-within-a-class, still used
    verbatim by SSP's own panel - test_semantic_search_p4.py pins that),
    PC_RegisterAll's list is the static PROVIDER_CLASSES registry - a
    type has no per-kind /_types data of its own to annotate with."""
    src = _catalog()
    reg = src[src.index("function PC_RegisterAll"):]
    reg = reg[:reg.index("\nfunction ", 1)]
    assert "useResource" not in reg, "a type list needs no fetch"
    assert "PROVIDER_CLASSES.map((cls) =>" in reg
    assert 'data-testid="provider-register-all-toggle"' in reg
    assert "onPick(cls.key)" in reg
    # PC_RegisterDropdown itself must survive untouched - SSP's own panel
    # depends on it (test_semantic_search_p4.py's own pin).
    assert "function PC_RegisterDropdown" in src


def test_register_all_is_the_top_level_control_not_gated_on_a_class() -> None:
    src = _catalog()
    assert "<PC_RegisterAll onPick={openCreate} />" in src


def test_picking_a_panel_type_switches_the_filter_not_a_form() -> None:
    """A panel class has no generic form to open - its own native create
    affordance already lives inside its own panel body."""
    src = _catalog()
    open_create = src[src.index("function openCreate"):]
    open_create = open_create[:open_create.index("\n  }\n")]
    assert 'k.form === "panel"' in open_create
    assert "selectClass(key)" in open_create


# ---------------------------------------------------------------------------
# Card click opens the SAME overlay in edit mode; create is the same
# overlay empty
# ---------------------------------------------------------------------------


def test_one_form_modal_serves_both_create_and_edit() -> None:
    src = _catalog()
    assert "const [formOpen, setFormOpen] = React.useState(false);" in src
    assert "const [editingRow, setEditingRow] = React.useState(null);" in src
    # Exactly one <Modal ...> render site in the whole file - not a
    # second, separate edit overlay.
    assert src.count("<Modal") == 1


def test_edit_prefills_and_locks_the_row_and_updates_via_put() -> None:
    src = _catalog()
    open_edit = src[src.index("function openEdit"):]
    open_edit = open_edit[:open_edit.index("\n  }\n")]
    assert "setDraft(row);" in open_edit

    save = src[src.index("const save = async (body) =>"):]
    save = save[:save.index("let body;")]
    assert 'const method = wasEditingId ? "PUT" : "POST";' in save
    assert "encodeURIComponent(wasEditingId)" in save


def test_card_open_hands_back_the_full_row_not_just_an_id() -> None:
    """An id alone cannot prefill an edit form - PC_InstanceCard's Open
    button must hand back the row itself."""
    card = _catalog()
    card = card[card.index("function PC_InstanceCard"):card.index("function PC_InstanceGrid")]
    assert "onClick={() => onOpen(row)}" in card


def test_form_receives_the_editing_flag() -> None:
    src = _catalog()
    assert "editing={!!editingRow}" in src


# ---------------------------------------------------------------------------
# Secrets never round-trip the mask; id is locked
# ---------------------------------------------------------------------------


def test_id_field_and_kind_select_are_disabled_while_editing() -> None:
    src = _form()
    # Both the id PC_Field and the provider <select> must be gated - not
    # just one of the two - so exactly two `disabled={editing}` JSX
    # props exist (a third, unrelated `disabled` guards Save on `busy`,
    # not `editing`, so this counts the literal prop, not the word).
    assert src.count("disabled={editing}") == 2
    id_block = src[src.index('field={PC_normalizeField({ key: "id"'):]
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


# ---------------------------------------------------------------------------
# Form spacing: a real two-column grid, not a flex row a fixed-width
# sibling can squeeze
# ---------------------------------------------------------------------------


def test_form_opens_in_a_wider_modal() -> None:
    src = _catalog()
    assert "width={720}" in src


def test_fields_and_probe_panel_are_a_real_css_grid() -> None:
    src = _form()
    assert 'display: "grid"' in src
    assert 'gridTemplateColumns: showProbePanel ? "minmax(0, 1fr) 240px" : "minmax(0, 1fr)"' in src
    # The old flex tug-of-war (flex: 1 fields vs. a fixed-width sibling
    # in the same flex row) must be gone, not just supplemented.
    assert 'className="row" style={{ gap: 20, alignItems: "flex-start" }}' not in src


# ---------------------------------------------------------------------------
# Kill the three-layer stack: the platform page mounts the catalog inline
# ---------------------------------------------------------------------------


def test_platform_page_mounts_the_catalog_inline_not_via_overlay() -> None:
    src = _platform()
    assert 'if (nav === "providers") return <NV_ProvidersPlatPage />;' in src
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
