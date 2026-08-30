"""01a052a5 item 5: real attachments flow, zero-backend default scope.

The lead's ruling: wire the composer's attach button to the ALREADY
WORKING upload primitive nv-files-sidebar.jsx uses (SH_api.fileUpload ->
PUT .../files, base64 - binary-safe), show pending uploads as removable
chips, and fold each finished upload's path into the sent instruction as
a plain "Attached file: {path}" line - no message-schema change. True
multimodal/vision attachments are a separate, explicitly-filed design
task (SteerBody has no parts/attachments field today - only a bare
`instruction: str | None`).

Static source checks (the ui/ suite convention) plus one MiniRacer test
for send()'s text-folding logic, extracted and executed for real -
mirrors test_trace_row_expand_toggle_via_mini_racer's "stub React, run
the real snippet" technique, since the folding rule (empty text vs. text
+ attachments vs. attachments-only, skipping non-"done" ones) is exactly
the kind of conditional a source-grep alone could get subtly wrong.
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


def test_send_folding_logic_via_mini_racer():
    """Extracts send()'s guard + fullText computation (up to the point
    it starts touching React state / the network) and runs it for real
    against representative attachment lists."""
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
        "function SEND_FOLD(val, attachments, sending, attachmentsPending) {\n"
        + body
        + "\n  return fullText;\n}\n"
    )
    transpiled = bundler._transform(wrapped, "send_fold_test.jsx")

    ctx = MiniRacer()
    ctx.eval(transpiled)

    def fold(val, attachments, sending=False, pending=False):
        # A guard hit returns bare `undefined`, which py_mini_racer's
        # JSON-marshalling ctx.call() cannot round-trip - coerce to null
        # before crossing back into Python.
        ctx.eval(
            "var __r = SEND_FOLD(" + json.dumps(val) + ", "
            + json.dumps(attachments) + ", " + json.dumps(sending) + ", "
            + json.dumps(pending) + ");"
        )
        return ctx.eval("__r === undefined ? null : __r")

    assert fold("hello", []) == "hello"
    assert fold("", []) is None, "nothing to send returns undefined (the guard's bare return)"
    assert fold("", [{"path": "uploads/x-a.png", "status": "done"}]) == (
        "Attached file: uploads/x-a.png"
    )
    assert fold("look at this", [{"path": "uploads/x-a.png", "status": "done"}]) == (
        "look at this\n\nAttached file: uploads/x-a.png"
    )
    two = fold("", [
        {"path": "uploads/x-a.png", "status": "done"},
        {"path": "uploads/x-b.pdf", "status": "done"},
    ])
    assert two == "Attached file: uploads/x-a.png\nAttached file: uploads/x-b.pdf"
    # An uploading/errored attachment is never folded into the sent text.
    assert fold("hi", [{"path": "uploads/x-a.png", "status": "uploading"}]) == "hi"
    assert fold("hi", [{"path": "uploads/x-a.png", "status": "error"}]) == "hi"
    # Only an uploading one blocks the guard when there is otherwise
    # nothing else to send yet.
    assert fold("", [{"path": "uploads/x-a.png", "status": "uploading"}]) is None
    assert fold("hello", [], sending=True) is None
    assert fold("hello", [], pending=True) is None
