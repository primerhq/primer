"""Platform wave P1b item 6/7: the new-workspace dialog and the Workspaces list's card anatomy (Rename-not-Delete footer).

Static-source checks only (the tests/ui suite convention).

Item 6 pinned the create modal of the Workspaces page (``WS_NewWorkspaceModal``: template ROWS, a "Label" field, an env "Overrides" box). Platform
revamp 08, Task 13 (design 14.3) replaced it with ONE dialog, the console's ``NV_CreateWorkspaceOverlay`` rebuilt on the platform kit, so the
item 6 pins below read that dialog now and pin its design instead: the template choice is the kit's searchable dropdown over every template, not rows
and not a native select; the extra environment is a kit row table whose values are secret-typed; and the body is built by ``NV_nwBody``. The
dialog's behaviour is pinned by ``tests/ui/test_new_workspace_dialog.py`` and ``tests/ui/test_nv_new_workspace_helpers.py``.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "workspaces.jsx"
OVERLAYS = UI / "components" / "console" / "nv-overlays.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _modal_src() -> str:
    """The new-workspace dialog, from its helper block to the end of the component."""
    src = OVERLAYS.read_text(encoding="utf-8")
    start = src.index("// ---- NV_nw pure helpers")
    end = src.index("// The management-surface mount table", src.index("function NV_CreateWorkspaceOverlay("))
    return src[start:end]


def _list_src() -> str:
    src = _src()
    start = src.index("function WorkspacesPage(")
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


# ---- Item 6: the create dialog --------------------------------------------


def test_modal_has_no_verb_chip() -> None:
    """The palette verb was a designer's annotation that shipped as UI (lead sweep L3)."""
    modal = _modal_src()
    assert "verb-chip" not in modal and "verb:" not in modal


def test_template_picker_is_the_kit_dropdown_not_rows_or_a_select() -> None:
    """Design 14.3: the template is picked from the kit's searchable selector at the top of the dialog, over every template (not the first 200)."""
    modal = _modal_src()
    assert "<PK_FormOverlay" in modal
    for piece in ('prefix: "From template"', "searchable: true", 'testid: "nv-nw-template"', "WT_fetchAllTemplates(apiFetch, signal)"):
        assert piece in modal, piece
    for gone in ("nv-nw-tpl:", "nv-pick-row", "<select", "?limit=200", "workspace-template-picker"):
        assert gone not in modal, gone


def test_the_summary_line_is_the_templates_own() -> None:
    """The rows' hand-composed descriptors are gone: the line under the dropdown is the one the templates page uses (WT_summaryLine: description,
    provider and size)."""
    modal = _modal_src()
    assert 'data-testid="nv-nw-summary"' in modal and "WT_summaryLine(template)" in modal
    assert "const parts = [" not in modal


def test_name_copy_is_verbatim() -> None:
    modal = _modal_src()
    assert 'label="Name"' in modal
    assert "Shown in the workspace menu in place of the id" in modal


def test_overrides_section_is_the_kit_row_table() -> None:
    modal = _modal_src()
    assert 'title: "EXTRA ENVIRONMENT"' in modal and 'testid: "nv-nw-env"' in modal and 'addLabel: "+ Add"' in modal


def test_overrides_value_input_is_secret_typed() -> None:
    modal = _modal_src()
    assert '{ key: "value", label: "Value", input: "secret" }' in modal


def test_overrides_footnote_is_the_designs_sentence() -> None:
    """Design 14.3's line under the extra environment (the old footnote spoke of mounts and a network policy the dialog no longer shows)."""
    modal = _modal_src()
    assert "Added to the template's; values are hidden" in modal
    assert "Env values are secret-typed" not in modal


def test_overrides_are_only_sent_when_a_key_is_present() -> None:
    """NV_nwBody builds the body: an env row with no name is left out, and so is an empty overrides (tests/ui/test_nv_new_workspace_helpers.py runs it)."""
    modal = _modal_src()
    assert "mutation.mutate(NV_nwBody(state))" in modal
    assert "if (named) overrides.env = env;" in modal
    assert "if (Object.keys(overrides).length) body.overrides = overrides;" in modal


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
