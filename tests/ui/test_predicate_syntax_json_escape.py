"""FB8 — SyntaxJson (predicate-builder.jsx) regex-wrapped JSON.stringify(value)
into innerHTML WITHOUT escaping, unlike the other five highlight sites. Assert
the value is HTML-escaped before injection so predicate content can't smuggle
markup.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
PB = UI / "components" / "predicate-builder.jsx"


def _src() -> str:
    return PB.read_text(encoding="utf-8")


def test_escape_helper_defined() -> None:
    src = _src()
    assert "function PB_escapeHtml(" in src
    # Escapes the three HTML-significant chars, ampersand first.
    assert '.replace(/&/g, "&amp;")' in src
    assert '.replace(/</g, "&lt;")' in src
    assert '.replace(/>/g, "&gt;")' in src


def test_syntax_json_escapes_before_injecting() -> None:
    src = _src()
    # The stringified value is escaped before the span-wrapping regex.
    assert "PB_escapeHtml(JSON.stringify(value, null, 2))" in src


def test_syntax_json_no_raw_stringify_into_html() -> None:
    """The old code assigned `const lines = JSON.stringify(value, null, 2);`
    then fed `lines` straight into innerHTML. Ensure the raw (unescaped) form
    is gone."""
    src = _src()
    # Isolate the SyntaxJson function body.
    m = re.search(r"function SyntaxJson\(\{ value \}\) \{.*?\n\}", src, re.S)
    assert m, "SyntaxJson function not found"
    body = m.group(0)
    assert "dangerouslySetInnerHTML" in body
    # `lines` must be the escaped value, never the bare stringify.
    assert "const lines = JSON.stringify(value, null, 2);" not in body
    assert "PB_escapeHtml(" in body


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
