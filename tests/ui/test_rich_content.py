"""Task E1 of docs/superpowers/plans/2026-07-05-chat-refactor.md — inline
artifact previews + collapsible sections (D6, §4.4).

Extends ``CT_AttachmentPart`` (ui/components/chat/transcript.jsx) so a
media part that carries only an ``artifact_id`` (no inline ``data`` — the
tool-produced-media shape persisted by
``primer/chat/executor.py::_tool_media_parts``; see
``tests/e2e/test_chat_artifact_fetch.py``) builds its ``src`` from the A8
byte-serving route (``GET /v1/chats/{chat_id}/artifacts/{artifact_id}``)
instead of requiring inline base64 ``data``. Images/PDFs render inline
with a thumb -> click-to-expand affordance; other file types render as a
rich chip (mime + best-effort size + open/download). A native
``<details>`` collapsible wraps long assistant markdown sections. Tool-
produced media (a ``tool_result`` row's ``payload.media``, flattened onto
the row per ``window.chatFlatten`` / ``_message_to_wire``) now also
renders inline via the same ``CT_AttachmentPart``, under the paired tool
row.

Static-source + transpile-build checks only (the ui/ suite convention,
e.g. test_tool_rendering.py / test_transcript_extracted.py) — no
DOM/browser harness.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
TRANSCRIPT = UI / "components" / "chat" / "transcript.jsx"


def _src() -> str:
    return TRANSCRIPT.read_text(encoding="utf-8")


def test_attachment_part_accepts_chat_id() -> None:
    src = _src()
    assert "function CT_AttachmentPart(" in src
    assert "chatId" in src


def test_artifact_url_built_from_chat_id_and_artifact_id_when_data_absent() -> None:
    src = _src()
    assert '"/v1/chats/"' in src
    assert '"/artifacts/"' in src
    assert "artifact_id" in src
    # This is a raw versioned URL for a real <img src>/<a href>/<embed
    # src>, not a data-layer fetch — apiFetch must stay out of it.
    assert "apiFetch" not in src


def test_inline_base64_path_still_supported() -> None:
    # Regression: the pre-A8 inline base64 path (no backend round-trip
    # needed) must keep working for a part that already carries `data`.
    src = _src()
    assert "base64,${" in src
    assert "part.data" in src


def test_pdf_renders_inline_via_embed_or_object() -> None:
    src = _src()
    assert ("<embed" in src) or ("<iframe" in src) or ("<object" in src)
    assert "application/pdf" in src


def test_image_preview_is_click_to_expand() -> None:
    src = _src()
    assert "function CT_ImagePreview(" in src
    assert "setExpanded" in src


def test_non_preview_files_render_as_a_rich_chip_with_type_and_size() -> None:
    src = _src()
    assert "function CT_FileChip(" in src
    assert "download" in src
    assert "CT_formatBytes" in src


def test_collapsible_details_used_for_long_assistant_sections() -> None:
    src = _src()
    assert "<details" in src
    assert "<summary" in src
    # Opt-in collapse, not opt-in reveal: stays expanded by default.
    assert " open" in src or "open>" in src or "open," in src


def test_tool_result_media_renders_inline_via_attachment_part() -> None:
    src = _src()
    assert ".media" in src
    assert "CT_AttachmentPart" in src


def test_transcript_still_pure_no_data_fetching_or_ws() -> None:
    src = _src()
    assert "new WebSocket(" not in src
    assert "apiFetch" not in src


def test_bundle_transpiles_with_rich_content_changes() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/chat/transcript.jsx === */" in text
