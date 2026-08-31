"""Structural + transpile checks for the vendored partial-JSON parser
(01a04725 - P4 prep: live tool-argument rendering).

``window.parsePartialJson(text) -> { value, state }`` is a first-party,
hand-written, never-throwing JSON repair used by the chat surface to
render tool-call arguments while the raw input text is still streaming
(Vercel AI SDK ``tool-input-delta``). ``JSON.parse`` runs first; on
failure a single linear scan repairs the tail (close an open string,
drop a partial value / trailing comma / incomplete key / partial
escape, and close the open containers) and re-parses.

  * state "complete"  - JSON.parse(text) succeeded verbatim.
  * state "repaired"  - the text was incomplete; a repaired copy parsed.
  * state "failed"    - nothing recoverable; value is undefined.

These tests are static-source + index.html-order checks plus
py_mini_racer executions of the actual parser to verify the repair
behaviour end-to-end rather than guessing at the scan from a substring
match (the ui/ suite convention, e.g. test_highlight_code_vendor.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
VENDOR = UI / "vendor"
INDEX = UI / "index.html"
PARSE_PARTIAL_JSON = VENDOR / "parse-partial-json.js"


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


def _ctx() -> "MiniRacer":
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(PARSE_PARTIAL_JSON.read_text(encoding="utf-8"))
    # Normalise a result into a JSON string; undefined -> a sentinel so
    # the "failed" (no value) cases are distinguishable from value:null.
    ctx.eval(
        "function check(t) {"
        " var r = window.parsePartialJson(t);"
        " var v = (r.value === undefined) ? '__UNDEF__' : r.value;"
        " return JSON.stringify({state: r.state, value: v});"
        "}"
    )
    return ctx


def test_parse_partial_json_file_exists_and_exports() -> None:
    assert PARSE_PARTIAL_JSON.exists(), "ui/vendor/parse-partial-json.js is missing"
    src = PARSE_PARTIAL_JSON.read_text(encoding="utf-8")
    assert "function parsePartialJson(" in src
    assert "window.parsePartialJson = parsePartialJson;" in src


def test_parse_partial_json_registered_in_index_html_after_markdown() -> None:
    order = _babel_order()
    assert "vendor/parse-partial-json.js" in order, (
        "parse-partial-json.js missing from ui/index.html babel block"
    )
    # Loaded after markdown.jsx (the vendor block it was appended to).
    assert order.index("vendor/markdown.jsx") < order.index(
        "vendor/parse-partial-json.js"
    )


def test_parse_partial_json_complete_and_repaired() -> None:
    ctx = _ctx()
    cases = [
        ('{"a": 1}', {"state": "complete", "value": {"a": 1}}),
        ('  {"a": 1}  ', {"state": "complete", "value": {"a": 1}}),
        ('{"a": "hel', {"state": "repaired", "value": {"a": "hel"}}),
        ('{"hel', {"state": "repaired", "value": {}}),
        ('{"a":', {"state": "repaired", "value": {}}),
        ("[1, 2.3e", {"state": "repaired", "value": [1]}),
        ("[1, tru", {"state": "repaired", "value": [1]}),
        ("[[1, 2", {"state": "repaired", "value": [[1, 2]]}),
        # An escaped quote before the cut is a complete escape: kept.
        ('{"a": "he\\"llo', {"state": "repaired", "value": {"a": 'he"llo'}}),
        # A partial unicode escape (\u + <4 hex) at the end is dropped.
        ('{"a": "ab\\u12', {"state": "repaired", "value": {"a": "ab"}}),
    ]
    for raw, expected in cases:
        got = json.loads(ctx.eval("check(" + json.dumps(raw) + ")"))
        assert got == expected, f"input {raw!r}: got {got}, expected {expected}"


def test_parse_partial_json_failed_cases() -> None:
    ctx = _ctx()
    # Empty input, garbage, and non-string inputs all fail to a value.
    for raw in ["", "1 2", "@#$"]:
        got = json.loads(ctx.eval("check(" + json.dumps(raw) + ")"))
        assert got == {"state": "failed", "value": "__UNDEF__"}, (
            f"input {raw!r}: got {got}"
        )
    # A non-string argument is rejected without throwing.
    got = json.loads(ctx.eval("check(null)"))
    assert got == {"state": "failed", "value": "__UNDEF__"}


def test_parse_partial_json_never_throws() -> None:
    ctx = _ctx()
    # Every case below must return a well-formed result, never throw.
    for raw in [
        "",
        "{",
        "[",
        '"',
        "{'a': 1, ",
        "{'a': \"x\\u12",
        "[1, 2, 3,",
        "{'a': 1}}",
        "1 2 3",
        "true",
    ]:
        out = ctx.eval("check(" + json.dumps(raw) + ")")
        parsed = json.loads(out)
        assert parsed["state"] in ("complete", "repaired", "failed"), (
            f"input {raw!r}: {parsed}"
        )
    ctx.eval("check(null)")  # non-string: no throw


def test_bundle_transpiles_with_parse_partial_json() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === vendor/parse-partial-json.js === */" in text
