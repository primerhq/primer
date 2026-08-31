"""Platform wave P1b item 6/7: workspaces.jsx create modal (verb chip,
Template row picker with real-field descriptors, Label copy, Overrides
section) and the list's card anatomy (Rename-not-Delete footer).

Static-source checks only (the tests/ui suite convention). Sister of
test_workspaces_mobile.py (grid-vs-table mechanism, already retargeted)
and test_workspace_create_loading_state.py (loading affordance,
unaffected by this wave) - this file covers the rest of the new P1b
surface those files don't.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "workspaces.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _modal_src() -> str:
    src = _src()
    start = src.index("function WS_NewWorkspaceModal(")
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


def _list_src() -> str:
    src = _src()
    start = src.index("function WorkspacesPage(")
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


# ---- Item 6: create modal -------------------------------------------------


def test_modal_has_a_verb_chip() -> None:
    modal = _modal_src()
    assert 'data-testid="workspace-modal-verb-chip"' in modal
    assert "verb: Create Workspace" in modal


def test_template_picker_is_rows_not_a_select() -> None:
    modal = _modal_src()
    assert 'data-testid="workspace-template-picker"' in modal
    assert 'data-testid={`workspace-template-row-${t.id}`}' in modal
    assert 'data-selected={t.id === templateId ? "true" : "false"}' in modal
    assert "<select" not in modal


def test_template_row_descriptor_uses_only_real_served_fields() -> None:
    """No invented descriptors: every part composed for the row comes
    from a field confirmed present on GET /workspace_templates list
    items (backend.kind, resources.network, files.length, env,
    strict_write_locking)."""
    modal = _modal_src()
    parts_start = modal.index("const parts = [")
    parts_end = modal.index("];", parts_start)
    parts_src = modal[parts_start:parts_end]
    for field in (
        "t.backend", "t.resources", "t.files", "t.env",
        "t.strict_write_locking",
    ):
        assert field in parts_src, f"{field} missing from the row descriptor"


def test_label_copy_is_verbatim() -> None:
    modal = _modal_src()
    assert "Label" in modal
    assert "shows in the selector" in modal


def test_overrides_section_has_add_and_remove() -> None:
    modal = _modal_src()
    assert 'data-testid="workspace-override-add"' in modal
    assert 'data-testid={`workspace-override-remove-${i}`}' in modal
    assert 'data-testid={`workspace-override-key-${i}`}' in modal
    assert 'data-testid={`workspace-override-value-${i}`}' in modal


def test_overrides_value_input_is_secret_typed() -> None:
    modal = _modal_src()
    key_idx = modal.index('data-testid={`workspace-override-value-${i}`}')
    row_start = modal.rindex("<input", 0, key_idx)
    row_src = modal[row_start:key_idx]
    assert 'type="password"' in row_src


def test_overrides_footnote_is_the_full_confirmed_sentence() -> None:
    """The brief hedged this might not exist server-side (#33
    report-first fallback: 'mounts-apply-first spirit without the env
    values clause'), but WorkspaceTemplateOverrides.env and
    WorkspaceCreate.overrides are both real and wired end-to-end - so
    the FULL sentence renders, not the fallback partial."""
    modal = _modal_src()
    assert 'data-testid="workspace-overrides-footnote"' in modal
    assert "Env values are secret-typed" in modal
    assert "mounts, network" in modal
    assert "init commands still apply first" in modal


def test_overrides_are_only_sent_when_a_key_is_present() -> None:
    modal = _modal_src()
    assert "if (row.key.trim()) env[row.key.trim()] = row.value;" in modal
    assert "if (Object.keys(env).length > 0) body.overrides = { env };" in modal


# ---- Item 7: card anatomy --------------------------------------------------


def test_list_is_a_card_grid() -> None:
    list_src = _list_src()
    assert 'data-testid="workspaces-grid"' in list_src
    assert "pc-card-grid" in list_src


def test_card_facts_use_only_confirmed_served_fields() -> None:
    """created_at and terminal_user_access were confirmed live via a
    GET /v1/workspaces?limit=1 - no invented "sessions count" fact
    (that field is not served)."""
    list_src = _list_src()
    assert "w.created_at" in list_src
    assert "w.terminal_user_access" in list_src
    assert "sessions" not in list_src.lower().split("pc-card-facts")[1].split(
        "pc-card-footer"
    )[0]


def test_card_footer_is_open_and_rename_not_delete() -> None:
    """Judgment call: destroy already lives behind WS_DestroyTab's own
    guarded confirmation flow in the detail view; a card-level
    quick-delete would bypass that guard rather than reuse it."""
    list_src = _list_src()
    assert 'data-testid={`workspace-card-open-${w.id}`}' in list_src
    assert 'data-testid={`workspace-card-rename-${w.id}`}' in list_src
    assert "workspace-card-delete-" not in list_src


def test_bundle_transpiles_with_workspaces_p1b() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/workspaces.jsx === */" in text
