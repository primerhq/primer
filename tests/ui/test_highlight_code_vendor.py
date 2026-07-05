"""Structural + transpile checks for the slim vendored code highlighter
(Task B1, docs/superpowers/plans/2026-07-05-chat-refactor.md).

``window.primerVendor.highlightCode(code, lang)`` is a first-party,
hand-written UMD highlighter mirroring highlight-json.js / highlight-
python.js: HTML-escape first, then wrap a slim token subset in
``<span style="color:var(--…)">``. json/python delegate to the existing
highlighters (loaded earlier in ui/index.html); js/ts/jsx and bash/sh get
a small tokeniser here; unknown langs fall back to a generic pass. It is
wired into vendor/markdown.jsx's fenced-code branch via
dangerouslySetInnerHTML, gated on ``window.primerVendor?.highlightCode``
existing so the fallback is graceful.

These tests are static-source + transpile-build checks only (the ui/
suite convention, e.g. test_graph_canvas_extracted.py /
test_studio_terminal.py) plus a couple of py_mini_racer executions of the
actual highlighter to verify escaping + delegation behavior end-to-end
rather than guessing at the regex from a substring match.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
VENDOR = UI / "vendor"
INDEX = UI / "index.html"
MANIFEST = VENDOR / "MANIFEST.md"
HIGHLIGHT_CODE = VENDOR / "highlight-code.js"
HIGHLIGHT_JSON = VENDOR / "highlight-json.js"
HIGHLIGHT_PYTHON = VENDOR / "highlight-python.js"
MARKDOWN = VENDOR / "markdown.jsx"


def _index_text() -> str:
    return INDEX.read_text(encoding="utf-8")


def _babel_order() -> list[str]:
    out: list[str] = []
    for line in _index_text().splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_highlight_code_file_exists_and_exports() -> None:
    assert HIGHLIGHT_CODE.exists(), "ui/vendor/highlight-code.js is missing"
    src = HIGHLIGHT_CODE.read_text(encoding="utf-8")
    assert "function highlightCode(" in src
    assert "window.primerVendor.highlightCode = highlightCode;" in src


def test_highlight_code_delegates_to_json_and_python() -> None:
    src = HIGHLIGHT_CODE.read_text(encoding="utf-8")
    assert "window.primerVendor.highlightJson" in src
    assert "window.primerVendor.highlightPython" in src


def test_highlight_code_registered_in_index_html_after_json() -> None:
    order = _babel_order()
    assert "vendor/highlight-code.js" in order
    assert order.index("vendor/highlight-json.js") < order.index("vendor/highlight-code.js")
    assert order.index("vendor/highlight-code.js") < order.index("vendor/markdown.jsx")


def test_vendor_manifest_documents_highlight_code() -> None:
    manifest = MANIFEST.read_text(encoding="utf-8")
    assert "highlight-code.js" in manifest, "highlight-code.js missing from vendor MANIFEST.md"
    digest = hashlib.sha256(HIGHLIGHT_CODE.read_bytes()).hexdigest()
    assert digest in manifest, f"highlight-code.js sha256 in MANIFEST.md is stale (got {digest})"
    assert "hand-written, no upstream" in manifest


def test_markdown_wires_highlight_code_with_graceful_degrade() -> None:
    src = MARKDOWN.read_text(encoding="utf-8")
    assert "window.primerVendor?.highlightCode" in src, "must gracefully degrade when the vendor fn is absent"
    assert "dangerouslySetInnerHTML" in src
    assert "highlightCode(body, lang)" in src or "highlightCode(" in src


def test_highlight_code_escapes_html_via_v8() -> None:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(HIGHLIGHT_JSON.read_text(encoding="utf-8"))
    ctx.eval(HIGHLIGHT_PYTHON.read_text(encoding="utf-8"))
    ctx.eval(HIGHLIGHT_CODE.read_text(encoding="utf-8"))

    assert ctx.eval("typeof window.primerVendor.highlightCode") == "function"

    html = ctx.eval('window.primerVendor.highlightCode("<script>alert(1)</script>", "js")')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html

    amp_html = ctx.eval('window.primerVendor.highlightCode("a && b", "bash")')
    assert "&amp;&amp;" in amp_html


def test_highlight_code_json_and_python_delegate_at_runtime() -> None:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(HIGHLIGHT_JSON.read_text(encoding="utf-8"))
    ctx.eval(HIGHLIGHT_PYTHON.read_text(encoding="utf-8"))
    ctx.eval(HIGHLIGHT_CODE.read_text(encoding="utf-8"))

    json_html = ctx.eval('window.primerVendor.highlightCode(\'{"a": 1}\', "json")')
    direct_json_html = ctx.eval('window.primerVendor.highlightJson(\'{"a": 1}\')')
    assert json_html == direct_json_html

    py_html = ctx.eval(
        'window.primerVendor.highlightCode("def f():\\n    return 1", "python")'
    )
    assert "<span" in py_html
    assert py_html.count("\n") == 1  # two source lines rejoined with a single "\n"


def test_bundle_transpiles_with_highlight_code() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === vendor/highlight-code.js === */" in text
