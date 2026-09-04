"""01a052a5 item 5 + 01a052d9 step 3: composer attachments, plain-text
convention plus the true-vision/document fold-split.

01a052a5's original scope: wire the composer's attach button to the
ALREADY WORKING upload primitive nv-files-sidebar.jsx uses
(SH_api.fileUpload -> PUT .../files, base64 - binary-safe), show pending
uploads as removable chips, and fold each finished upload's path into the
sent instruction as a plain "Attached file: {path}" line - no message-
schema change.

01a052d9 step 3 (this file's newer half): SteerBody now HAS a real
attachments field (list[AttachmentIn], each a bare {path}) that folds a
workspace file into the turn as true vision/document input
(ImagePart/DocumentPart via primer.channel.media.media_from_workspace_
files), landed on main as part of the same design. send() now SPLITS
readyAttachments by NV_isVisionDocAttachment(a.type): image/* and the
handful of document mimes ride as steerAttachments (SteerBody.
attachments); everything else (text/code/audio/video/unrecognised) keeps
the original plain-text convention above, since the agent's existing
file tools already handle those and there is no benefit to hydrating
their bytes into the prompt.

Static source checks (the ui/ suite convention) plus one MiniRacer test
for send()'s text-folding + fold-split logic, extracted and executed for
real - mirrors test_trace_row_expand_toggle_via_mini_racer's "stub React,
run the real snippet" technique, since the folding/split rule (empty
text vs. text + attachments vs. attachments-only, skipping non-"done"
ones, routing by mime type) is exactly the kind of conditional a source-
grep alone could get subtly wrong.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
DOC = (UI / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")


def _composer() -> str:
    start = DOC.index("function NV_Composer")
    end = DOC.index("\n// ---", start)
    return DOC[start:end]


def _vision_doc_helper() -> str:
    """NV_isVisionDocAttachment + its NV_VISION_DOC_EXACT_TYPES constant --
    module-level, defined just above NV_Composer, which calls it. The
    MiniRacer harness below needs this alongside the extracted composer
    body since it isn't a closure-captured local."""
    start = DOC.index("var NV_VISION_DOC_EXACT_TYPES")
    end = DOC.index("\n\n// ---", start)
    return DOC[start:end]


def test_attach_button_no_longer_fires_the_stub_toast():
    composer = _composer()
    assert "Attachments land with the upload flow polish" not in composer


def test_attach_button_opens_a_hidden_file_input():
    composer = _composer()
    assert 'data-testid="nv-attach-input"' in composer
    assert 'type: "file"' in composer or 'type="file"' in composer
    assert "attachInputRef.current.click()" in composer


def test_hidden_file_input_lives_outside_the_nv_composer_container():
    """Regression pin: test_shell_journeys.py::
    test_send_and_steer_keep_the_composer_writable locates the composer's
    text field via `get_by_test_id("nv-composer").locator("input, textarea")`
    - a generic tag-based locator that matched BOTH the real textarea and
    this hidden upload <input> when it lived inside that container
    (strict-mode violation, "resolved to 2 elements"). Fixed at the
    source (not a test-side :not([type=file]) patch, which would leave
    every OTHER generic locator against nv-composer with the same trap):
    the file input renders as a sibling of the nv-composer div, inside a
    wrapping Fragment, so it is structurally never a descendant of the
    testid container regardless of what a generic locator selects for."""
    composer = _composer()
    attach_input_at = composer.index('data-testid="nv-attach-input"')
    composer_wrap_at = composer.index('data-testid="nv-composer"')
    assert attach_input_at < composer_wrap_at, (
        "the hidden file input must render BEFORE (i.e. outside) the "
        "nv-composer container opens"
    )
    assert "<React.Fragment>" in composer


def test_attach_reuses_the_existing_upload_primitive():
    """Same call shape as nv-files-sidebar.jsx's own upload() - no new
    backend endpoint, no new wire format."""
    composer = _composer()
    assert "reader.readAsDataURL(f)" in composer
    assert 'String(reader.result).split(",")[1]' in composer
    assert "SH_api.fileUpload(con.wid, dest, b64)" in composer
    assert '"uploads/" + token + "-" + f.name' in composer


def test_attachment_statuses_cover_uploading_done_and_error():
    composer = _composer()
    assert '"uploading"' in composer
    assert '"done"' in composer
    assert '"error"' in composer


def test_chips_are_removable():
    composer = _composer()
    assert 'data-testid="nv-attach-chips"' in composer
    chip = composer[composer.index('data-testid="nv-attach-chips"'):]
    assert 'data-testid={"nv-attach-remove:" + a.id}' in chip
    assert "removeAttachment(a.id)" in chip
    assert "nv-attach-chip-name" in chip


def test_send_is_blocked_while_any_upload_is_still_in_flight():
    composer = _composer()
    assert "attachmentsPending" in composer
    assert "disabled={sending || attachmentsPending}" in composer


def test_attachments_clear_only_on_successful_send():
    """Mirrors the existing draft-restore-on-failure behaviour (P0): a
    failed send must leave attachments in place for retry, not silently
    drop them - setAttachments([]) may appear only in the success
    branch, never in the failure handler."""
    composer = _composer()
    send_fn = composer[composer.index("function send() {"):]
    send_fn = send_fn[:send_fn.index("\n  function micStart")]
    success_branch = send_fn[:send_fn.index("}, function (err) {")]
    failure_branch = send_fn[send_fn.index("}, function (err) {"):]
    assert "setAttachments([])" in success_branch
    assert "setAttachments([])" not in failure_branch


def _send_fold_context():
    """Build the MiniRacer context + `fold()` helper shared by the
    folding and fold-split tests below. Extracts send()'s guard +
    fullText/steerAttachments computation (up to the point it starts
    touching React state / the network) and runs it for real against
    representative attachment lists.

    Returns ``fold(val, attachments, sending=False, pending=False)``,
    which gives back ``{"fullText": ..., "steerAttachments": ...}`` (both
    ``None`` when absent) or bare ``None`` when the guard's own early
    ``return;`` fired (nothing to send).
    """
    import json

    from py_mini_racer import MiniRacer

    from primer.api._jsx_bundle import JSXBundler

    bundler = JSXBundler(
        ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text(encoding="utf-8"),
    )
    composer = _composer()
    start = composer.index("function send() {") + len("function send() {")
    end = composer.index("setSending(true);", start)
    body = composer[start:end]
    wrapped = (
        _vision_doc_helper() + "\n"
        "function SEND_FOLD(val, attachments, sending, attachmentsPending) {\n"
        + body
        + "\n  return JSON.stringify({"
        "fullText: (typeof fullText === \"undefined\" ? null : fullText), "
        "steerAttachments: (typeof steerAttachments === \"undefined\" "
        "? null : steerAttachments)"
        "});\n}\n"
    )
    transpiled = bundler._transform(wrapped, "send_fold_test.jsx")

    ctx = MiniRacer()
    ctx.eval(transpiled)

    def fold(val, attachments, sending=False, pending=False):
        # A guard hit returns bare `undefined` for the whole function
        # (SEND_FOLD's inlined `return;` never reaches the JSON.stringify
        # tail below it) - coerce to None before crossing back into
        # Python, same as the pre-fold-split version of this test did.
        ctx.eval(
            "var __r = SEND_FOLD(" + json.dumps(val) + ", "
            + json.dumps(attachments) + ", " + json.dumps(sending) + ", "
            + json.dumps(pending) + ");"
        )
        raw = ctx.eval("__r === undefined ? null : __r")
        return json.loads(raw) if raw is not None else None

    return fold


def test_send_folding_logic_via_mini_racer():
    fold = _send_fold_context()

    def text_of(val, attachments, **kw):
        r = fold(val, attachments, **kw)
        return r["fullText"] if r else None

    assert text_of("hello", []) == "hello"
    assert fold("", []) is None, "nothing to send returns undefined (the guard's bare return)"
    assert text_of("", [{"path": "uploads/x-a.pdf", "status": "done", "type": "text/plain"}]) == (
        "Attached file: uploads/x-a.pdf"
    )
    assert text_of(
        "look at this",
        [{"path": "uploads/x-a.pdf", "status": "done", "type": "text/plain"}],
    ) == "look at this\n\nAttached file: uploads/x-a.pdf"
    two = text_of("", [
        {"path": "uploads/x-a.txt", "status": "done", "type": "text/plain"},
        {"path": "uploads/x-b.csv", "status": "done", "type": "text/csv"},
    ])
    assert two == "Attached file: uploads/x-a.txt\nAttached file: uploads/x-b.csv"
    # An uploading/errored attachment is never folded into the sent text.
    assert text_of("hi", [{"path": "uploads/x-a.txt", "status": "uploading", "type": "text/plain"}]) == "hi"
    assert text_of("hi", [{"path": "uploads/x-a.txt", "status": "error", "type": "text/plain"}]) == "hi"
    # Only an uploading one blocks the guard when there is otherwise
    # nothing else to send yet.
    assert fold("", [{"path": "uploads/x-a.txt", "status": "uploading", "type": "text/plain"}]) is None
    assert fold("hello", [], sending=True) is None
    assert fold("hello", [], pending=True) is None


def test_send_fold_split_routes_images_and_documents_as_vision_attachments():
    """01a052d9 step 3: image/* and document mimes ride as
    steerAttachments (SteerBody.attachments -> true vision/document
    input); everything else keeps the plain-text convention. Mixed lists
    split correctly, and a vision-only attachment with no typed text
    still has something to send (the guard does not block it)."""
    fold = _send_fold_context()

    image_only = fold("", [
        {"path": "uploads/x-a.png", "status": "done", "type": "image/png"},
    ])
    assert image_only is not None, "a vision attachment alone must not hit the empty-send guard"
    assert image_only["fullText"] == ""
    assert image_only["steerAttachments"] == [{"path": "uploads/x-a.png"}]

    pdf_only = fold("", [
        {"path": "uploads/report.pdf", "status": "done", "type": "application/pdf"},
    ])
    assert pdf_only["steerAttachments"] == [{"path": "uploads/report.pdf"}]
    assert pdf_only["fullText"] == ""

    # A text/code file stays on the legacy convention, not steerAttachments.
    text_only = fold("", [
        {"path": "uploads/notes.txt", "status": "done", "type": "text/plain"},
    ])
    assert text_only["steerAttachments"] is None
    assert text_only["fullText"] == "Attached file: uploads/notes.txt"

    # A mixed list splits: the image goes to steerAttachments, the text
    # file stays folded into fullText - and only the text file appears
    # in fullText, not both.
    mixed = fold("caption", [
        {"path": "uploads/photo.jpg", "status": "done", "type": "image/jpeg"},
        {"path": "uploads/readme.txt", "status": "done", "type": "text/plain"},
    ])
    assert mixed["steerAttachments"] == [{"path": "uploads/photo.jpg"}]
    assert mixed["fullText"] == "caption\n\nAttached file: uploads/readme.txt"

    # An uploading/errored image is not yet "done" - it must not appear
    # in steerAttachments any more than it would in the old fullText fold.
    still_uploading = fold("hi", [
        {"path": "uploads/x-a.png", "status": "uploading", "type": "image/png"},
    ])
    assert still_uploading["steerAttachments"] is None
    assert still_uploading["fullText"] == "hi"
