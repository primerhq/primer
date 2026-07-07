"""studio-ux fix 5 — the "New workspace" modal's Create button
(`WS_NewWorkspaceModal` in ui/components/workspaces.jsx) gave no feedback
while the POST + (slow, k8s) provisioning ran: it was already disabled
in-flight (`disabled={!templateId || create.loading}`), but with no visual
loading affordance an operator could easily read the button as inert/broken
and click again or bail out. It now shows an inline spinner + "Creating…"
label while `create.loading` (the existing useMutation() state) is true.

Static-source checks only (the tests/ui suite convention).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
WORKSPACES = UI / "components" / "workspaces.jsx"
STYLES = UI / "styles.css"


def _fn_block(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


def _modal_src() -> str:
    src = WORKSPACES.read_text(encoding="utf-8")
    return _fn_block(src, "function WS_NewWorkspaceModal(", "function WorkspaceDetail(")


def test_create_button_stays_disabled_while_the_mutation_is_in_flight() -> None:
    modal = _modal_src()
    assert "disabled={!templateId || create.loading}" in modal


def test_create_button_shows_a_spinner_and_creating_label_while_loading() -> None:
    modal = _modal_src()
    assert 'data-testid="workspace-create-submit"' in modal
    assert '{create.loading ? (<><span className="spinner" /><span>Creating…</span></>) : "Create"}' in modal


def test_create_button_swaps_its_icon_out_for_the_spinner_while_loading() -> None:
    # The Btn shell always renders `icon` as a leading <Icon>; passing
    # icon={undefined} while loading avoids a duplicate plus-icon sitting
    # next to the spinner.
    modal = _modal_src()
    assert 'icon={create.loading ? undefined : "plus"}' in modal


def test_create_uses_the_existing_use_mutation_loading_state() -> None:
    # No new bespoke "submitting" state — reuses useMutation()'s own
    # .loading, exactly as instructed.
    modal = _modal_src()
    assert "const create = useMutation(" in modal
    assert "create.loading" in modal


def test_spinner_css_is_shared_beyond_the_auth_submit_button() -> None:
    css = STYLES.read_text(encoding="utf-8")
    assert ".auth-submit .spinner,\n.btn .spinner {" in css


def test_bundle_transpiles_with_workspace_create_loading_state() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/workspaces.jsx === */" in text
