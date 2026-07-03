"""The Studio "New session" create form was enlarged from a small positioned
overlay into a proper centered Modal (reusing the shared ``Modal`` from
shared.jsx) with a LARGE multi-line instructions box, and gained a multi-file
"Attach files" control whose files are base64-uploaded into the workspace
(``attachments/<name>``) BEFORE the session is created and then referenced in
the initial instructions so the agent knows about them.

These are structural (source-text) checks, matching the style of the other
new-session-form tests in this suite.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SHARED_FORM = UI / "components" / "new-session-form.jsx"


def _src() -> str:
    return SHARED_FORM.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Enlarged into a proper centered Modal
# ---------------------------------------------------------------------------


def test_form_renders_via_shared_modal() -> None:
    src = _src()
    # The form body is rendered inside the shared <Modal> (centered overlay
    # with Escape / backdrop-click / Cancel close), titled "New session".
    assert "<Modal" in src
    assert 'title="New session"' in src
    # The old positioned "inline" overlay chrome is gone.
    assert 'if (variant === "inline")' not in src
    assert 'position: "absolute"' not in src


def test_modal_is_comfortably_wide() -> None:
    src = _src()
    # A width override opts out of the default 420px .modal cap so the form is
    # wide enough to paste a detailed prompt.
    assert 'width="min(94vw, 640px)"' in src


def test_instructions_textarea_is_large() -> None:
    src = _src()
    # The Initial instructions box is multi-line, tall, and resizable.
    assert 'data-testid="new-session-instructions"' in src
    assert "rows={8}" in src
    assert 'resize: "vertical"' in src


def test_form_keeps_testid_and_name_field() -> None:
    src = _src()
    # Testids preserved through the enlargement.
    assert 'data-testid="new-session-form"' in src
    assert 'data-testid="new-session-name"' in src


# ---------------------------------------------------------------------------
# 2. Multi-file "Attach files" control
# ---------------------------------------------------------------------------


def test_attach_input_is_multi_file() -> None:
    src = _src()
    # A multi-file <input type="file"> exists for picking attachments.
    assert 'data-testid="new-session-attach-input"' in src
    assert 'type="file"' in src
    assert "multiple" in src


def test_attach_list_and_remove_exist() -> None:
    src = _src()
    # Picked files are listed, each with a remove control.
    assert 'data-testid="new-session-attach-list"' in src
    assert 'data-testid="new-session-attach-item"' in src
    assert 'data-testid="new-session-attach-remove"' in src
    assert "removeAttachment(" in src


# ---------------------------------------------------------------------------
# 3. Create uploads (base64 PUT) BEFORE creating the session + references paths
# ---------------------------------------------------------------------------


def test_upload_is_base64_put_before_create() -> None:
    src = _src()
    up = src.index('"PUT"')
    create = src.index("create.mutate(body)")
    assert up < create, "attachments must be PUT-uploaded before the session is created"
    # Base64 upload to the workspace files API, under attachments/.
    assert 'encoding: "base64"' in src
    assert '"attachments/" + att.name' in src
    assert "SharedNewSessionFileToBase64" in src


def test_upload_failure_blocks_session_create() -> None:
    src = _src()
    # A failed upload surfaces a toast and returns WITHOUT creating the session.
    assert "Attachment upload failed" in src
    # The early-return guard sits before the create call in source order.
    guard = src.index("submittingRef.current = false;\n        return;")
    create = src.index("create.mutate(body)")
    assert guard < create


def test_uploaded_paths_referenced_in_instructions() -> None:
    src = _src()
    # The uploaded paths are appended to initial_instructions.
    assert '"Attached files: "' in src
    assert "uploadedPaths" in src
    assert "body.initial_instructions = instrText" in src


def test_bundle_transpiles_with_enlarged_form() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    assert "/* === components/new-session-form.jsx === */" in body.decode("utf-8")
