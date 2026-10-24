"""The canvas frame shell (pivot P5, Task 13; design 8.6): one static page that inlines its own assets and is versioned by its content.

The frame's CSP header is the ONLY script control of the canvas (audit G8-06, the lead's ruling 5 on S6): the Html script check is an early
warning, never a gate. So the shell must not carry a second control (a ``<meta>`` policy, a nonce, a hash) or load anything from elsewhere, and
every Html element must be a ``srcdoc`` frame of the shell: an ``about:srcdoc`` document inherits the policy container of the document that
made it (HTML Standard, "creating a new browsing context"), so the header governs each Html frame too. A frame made from a ``data:`` URL or a
``blob:`` URL, or one given ``allow-same-origin``, would not be bound the same way, and the scan below refuses every file of the frame that
names one. The browser pin (a script from a host that is not allowed raises a CSP violation inside an Html frame) is Task 26's journey.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from primer.api._app_middleware import _UI_DIR
from primer.canvas.frame import FRAME_ASSETS, build_frame_shell, frame_csp, frame_headers, frame_url

CANVAS_UI = _UI_DIR / "canvas"

#: What would put a frame outside the header's reach, or the frame outside its own sandbox.
_ESCAPES = {
    "allow-same-origin": re.compile(r"allow-same-origin", re.IGNORECASE),
    "a data: document": re.compile(r"data:\s*text/html", re.IGNORECASE),
    "a blob: document": re.compile(r"createObjectURL", re.IGNORECASE),
    "a javascript: URL": re.compile(r"javascript:", re.IGNORECASE),
    "a second policy": re.compile(r"http-equiv\s*=\s*[\"']?content-security-policy", re.IGNORECASE),
    "a nonce or hash": re.compile(r"\bnonce\s*=|'(?:sha256|sha384|sha512|nonce)-", re.IGNORECASE),
}


def _shell_dir(tmp_path: Path, renderer: str = "window.CV = {version: 1, boot: function () {}};\n") -> Path:
    ui = tmp_path / "ui"
    (ui / "canvas").mkdir(parents=True)
    (ui / "canvas" / "frame.html").write_text((CANVAS_UI / "frame.html").read_text(encoding="utf-8"), encoding="utf-8")
    (ui / "canvas" / "frame.css").write_text("body { margin: 0; }\n", encoding="utf-8")
    (ui / "canvas" / "renderer.js").write_text(renderer, encoding="utf-8")
    return ui


def test_the_shell_inlines_its_assets_and_versions_them(tmp_path):
    ui = _shell_dir(tmp_path)
    shell = build_frame_shell(ui)
    body = shell.body.decode("utf-8")
    assert "body { margin: 0; }" in body and "window.CV = {version: 1" in body
    assert "/*__CANVAS_CSS__*/" not in body and "/*__CANVAS_RENDERER__*/" not in body
    assert re.fullmatch(r"[0-9a-f]{16}", shell.version)
    assert build_frame_shell(ui) == shell, "the same files give the same shell"

    (ui / "canvas" / "renderer.js").write_text("window.CV = {version: 2, boot: function () {}};\n", encoding="utf-8")
    changed = build_frame_shell(ui)
    assert changed.version != shell.version and b"version: 2" in changed.body, "a changed renderer.js gives another version"
    assert frame_url(ui) == f"/canvas/frame?v={changed.version}"


def test_a_template_without_its_placeholders_is_refused(tmp_path):
    ui = _shell_dir(tmp_path)
    (ui / "canvas" / "frame.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    with pytest.raises(ValueError, match="__CANVAS_CSS__"):
        build_frame_shell(ui)


def test_the_repo_shell_builds():
    shell = build_frame_shell(_UI_DIR)
    assert shell.body.startswith(b"<!doctype html>") and b"window.CV" in shell.body


def test_the_header_is_the_shells_only_script_control():
    """Ruling 5 on S6, the template half: nothing in the shell adds, loosens or replaces the header's policy, and nothing loads from
    elsewhere into it (every script and style is inline)."""
    body = build_frame_shell(_UI_DIR).body.decode("utf-8")
    assert not re.search(r"<meta[^>]+http-equiv", body, re.IGNORECASE), "a <meta> policy would be a second control"
    assert not re.search(r"<script[^>]*\bsrc\s*=", body, re.IGNORECASE), "the shell loads no script from elsewhere"
    assert not re.search(r"<link\b", body, re.IGNORECASE), "the shell loads no stylesheet from elsewhere"
    assert not re.search(r"<base\b", body, re.IGNORECASE)
    for name, pattern in _ESCAPES.items():
        assert not pattern.search(body), f"the shell carries {name}"


@pytest.mark.parametrize("name", FRAME_ASSETS)
def test_no_file_of_the_frame_makes_a_frame_the_header_does_not_bind(name):
    """Ruling 5 on S6, the inheritance half: every Html element is a ``srcdoc`` frame, which inherits the header's policy. A file of the
    frame never names a ``data:`` or ``blob:`` document, a ``javascript:`` URL, ``allow-same-origin``, a second policy or a nonce. The
    scan covers every file the shell inlines, the renderer that S7 writes and the Html bootstrap included."""
    path = CANVAS_UI / name
    if not path.exists():
        pytest.skip(f"{name} is added by a later task")
    text = path.read_text(encoding="utf-8")
    for what, pattern in _ESCAPES.items():
        assert not pattern.search(text), f"{name} names {what}"


def test_every_file_under_ui_canvas_is_an_asset_the_scan_covers():
    """A new file of the frame joins ``FRAME_ASSETS`` (and so the scan above), or the scan would miss it."""
    assert {p.name for p in CANVAS_UI.iterdir() if p.is_file()} <= set(FRAME_ASSETS)


def test_the_policy_sandboxes_the_frame_without_its_origin():
    csp = frame_csp(own_origin=None)
    directives = [d.strip() for d in csp.split(";")]
    assert directives[0] == "sandbox allow-scripts allow-forms"
    assert "default-src 'none'" in directives and "connect-src 'none'" in directives and "object-src 'none'" in directives
    assert [d.split()[0] for d in directives].count("script-src") == 1, "one script-src: a duplicate would be ignored, or win"


def test_the_headers_are_the_designs():
    cached = frame_headers(version_ok=True, own_origin=None)
    assert cached == {
        "Content-Security-Policy": frame_csp(own_origin=None),
        "X-Frame-Options": "SAMEORIGIN",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "public, max-age=31536000, immutable",
    }
    assert frame_headers(version_ok=False, own_origin=None)["Cache-Control"] == "no-store"


def test_an_http_origin_joins_img_and_media_only():
    csp = frame_csp(own_origin="http://primer.local:9000")
    assert "img-src https: data: blob: http://primer.local:9000;" in csp
    assert "media-src https: data: blob: http://primer.local:9000;" in csp
    assert csp.count("http://primer.local:9000") == 2
