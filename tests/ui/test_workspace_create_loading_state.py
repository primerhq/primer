"""studio-ux fix 5, retargeted to the one new-workspace dialog (platform revamp 08, Task 13; design 14.3).

The Create button of the old Workspaces page's modal (``WS_NewWorkspaceModal``) gave no feedback while the POST and the (slow, Kubernetes) provisioning
ran; fix 5 gave it a spinner and "Creating…", and it was natively ``disabled`` for the length of the request. That modal is gone: the console's
``NV_CreateWorkspaceOverlay`` (``ui/components/console/nv-overlays.jsx``) is the one dialog, and its Create says it is busy the kit's way: ``Btn busy``
(``aria-busy`` and ``aria-disabled``, the click refused, the focus kept; ``docs/dev/subsystems/ui-foundation.md``), never ``disabled`` for a request
(``tests/ui/test_busy_buttons_ratchet.py``). ``disabled`` is kept for "cannot be sent as typed" (no template, or an error in the form). The behaviour is
pinned by ``tests/ui/test_new_workspace_dialog.py::test_create_is_busy_not_disabled_while_the_request_is_out``.

Static-source checks only (the tests/ui suite convention).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
OVERLAYS = UI / "components" / "console" / "nv-overlays.jsx"
STYLES = UI / "styles.css"


def _dialog_src() -> str:
    src = OVERLAYS.read_text(encoding="utf-8")
    start = src.index("function NV_CreateWorkspaceOverlay(")
    return src[start:src.index("// The management-surface mount table", start)]


def _create_button() -> str:
    dialog = _dialog_src()
    at = dialog.index('data-testid="nv-nw-create"')
    return dialog[dialog.rindex("<Btn", 0, at):dialog.index("</Btn>", at)]


def test_create_is_busy_while_the_request_is_out_and_says_creating() -> None:
    button = _create_button()
    assert "busy={busy}" in button
    assert '{busy ? "Creating…" : "Create"}' in button


def test_create_is_never_disabled_by_the_request() -> None:
    button = _create_button()
    disabled = re.search(r"disabled=\{([^}]*)\}", button)
    assert disabled and disabled.group(1) == "!ready", button
    assert "var ready = !!template && Object.keys(errs).length === 0;" in _dialog_src(), "ready says what can be sent, not whether a request is out"


def test_busy_is_the_mutations_own_loading_state() -> None:
    """No bespoke "submitting" flag for the button: it reads useMutation()'s .loading (a ref only stops a second click before that has rendered)."""
    dialog = _dialog_src()
    assert "var mutation = api.useMutation(" in dialog
    assert "var busy = !!mutation.loading;" in dialog


def test_cancel_is_held_off_not_disabled_while_create_is_out() -> None:
    dialog = _dialog_src()
    at = dialog.index('data-testid="nv-nw-cancel"')
    cancel = dialog[dialog.rindex("<Btn", 0, at):dialog.index("</Btn>", at)]
    assert "blocked={busy}" in cancel and "disabled=" not in cancel


def test_spinner_css_is_shared_beyond_the_auth_submit_button() -> None:
    css = STYLES.read_text(encoding="utf-8")
    assert ".auth-submit .spinner,\n.btn .spinner {" in css


def test_bundle_transpiles_with_the_new_workspace_dialog() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/console/nv-overlays.jsx === */" in text
