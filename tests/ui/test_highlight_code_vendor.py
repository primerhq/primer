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

UI-H2 (2026-10-31 perf review): the tokenisers used to run one regex pass
per token class over the HTML the earlier passes had already wrapped, so
the string pass wrapped a "color:var(--...)" attribute and the number pass
matched the 4 in --text-4, and the console showed the CSS fragments as
text. They now run one pass per language over the escaped source, the
alternation in the old pass order (comment, string, number, keyword), so a
class can never claim a region inside a span a class before it wrapped.
Pinned here by the visible-text invariant, the flat well-formed span
shape, the byte-identical snapshots of inputs that rendered correctly
before, the workspaces.jsx delegation check, and the no-on* fuzz.

UI-H3 (same review): window.renderMarkdown returns React children, not an
HTML string; the collections read-only pane passed that array through
dangerouslySetInnerHTML's __html and rendered "[object Object]". Pinned
here by a static check (no ui/**/*.jsx passes renderMarkdown through
__html) and a MiniRacer mount of the read-only KN_DocumentPane with the
real renderMarkdown loaded, which must draw a real heading element.
"""

from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# UI-H2: the single-pass tokenisers must keep the visible text intact.
# ---------------------------------------------------------------------------

WORKSPACES = UI / "components" / "workspaces.jsx"


def _v8_highlighter():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    for f in (HIGHLIGHT_JSON, HIGHLIGHT_PYTHON, HIGHLIGHT_CODE):
        ctx.eval(f.read_text(encoding="utf-8"))
    return ctx


def _strip_markup(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


_H2_INVARIANTS = [
    ("const a = 1; // c", "js"),
    ("echo \"hi\" # c", "bash"),
    ("x = 'a'", ""),
    ("x = 1  # hi", "python"),
    ('{"a": 1}', "json"),
]


@pytest.mark.parametrize("code, lang", _H2_INVARIANTS)
def test_highlight_code_keeps_visible_text(code, lang) -> None:
    ctx = _v8_highlighter()
    out = ctx.call("window.primerVendor.highlightCode", code, lang)
    assert _strip_markup(out) == code, f"lang={lang!r} changed the visible text: {out!r}"


_SPAN_OPENER = re.compile(r'<span style="color:var\(--(?:violet|green|amber|blue|text-4)\)(;font-style:italic)?">')


@pytest.mark.parametrize("code, lang", _H2_INVARIANTS)
def test_highlight_code_emits_flat_well_formed_spans(code, lang) -> None:
    # Every tag is one of the highlighter's own openers or its close, and
    # spans never nest: one pass cannot claim a region inside a region it
    # already wrapped (the old corruption produced <span style=<span ...>).
    ctx = _v8_highlighter()
    out = ctx.call("window.primerVendor.highlightCode", code, lang)
    depth = 0
    for part in re.split(r"(<[^>]+>)", out):
        if not part.startswith("<"):
            continue
        if part == "</span>":
            depth -= 1
        else:
            assert _SPAN_OPENER.fullmatch(part), f"unexpected tag {part!r} in {out!r}"
            depth += 1
        assert 0 <= depth <= 1, f"nested or unbalanced spans in {out!r}"


# Characterization: inputs that rendered correctly before the rewrite keep
# byte-identical output.
_H2_SNAPSHOTS = [
    ("const x = y;", "js", '<span style="color:var(--violet)">const</span> x = y;'),
    ("echo hello", "bash", '<span style="color:var(--violet)">echo</span> hello'),
    ("ls -la", "bash", 'ls <span style="color:var(--blue)">-la</span>'),
    ("return x", "", '<span style="color:var(--violet)">return</span> x'),
    ("def f():", "python", '<span style="color:var(--violet)">def</span> f():'),
    ('{"a": 1}', "json", '{<span style="color:var(--blue)">"a"</span>: <span style="color:var(--amber)">1</span>}'),
]


@pytest.mark.parametrize("code, lang, expected", _H2_SNAPSHOTS)
def test_highlight_code_snapshot_of_correct_inputs(code, lang, expected) -> None:
    ctx = _v8_highlighter()
    assert ctx.call("window.primerVendor.highlightCode", code, lang) == expected


def _ws_code_highlight_body(src: str) -> str:
    start = src.index("function WS_CodeHighlight")
    nxt = src.find("\nfunction ", start + 1)
    return src[start : nxt if nxt != -1 else len(src)]


def test_ws_code_highlight_delegates_to_the_vendor_highlighter() -> None:
    # The workspace file viewer used to inline the same multi-pass
    # highlighter; it must call the single-pass vendor highlightPython.
    body = _ws_code_highlight_body(WORKSPACES.read_text(encoding="utf-8"))
    assert "window.primerVendor?.highlightPython" in body
    assert "var(--amber)" not in body and "var(--violet)" not in body, (
        "the inline token passes are gone; only the line numbers keep a colour"
    )


def test_highlight_code_fuzz_emits_no_on_attributes() -> None:
    # The review's hlfuzz, ported: 20000 seeded random inputs (the same
    # tokens and seed), three langs each. The output goes straight into
    # innerHTML, so no output may carry an on* attribute inside a tag.
    ctx = _v8_highlighter()
    js = r"""
var toks = ['"', "'", '//', '#', '1', ' onmouseover=alert(1) ', 'var', 'x=', '\n', ' ', '/*', '*/', '`', 'a', '-x', 'if', '=', 'style'];
var seed = 7;
var rnd = function (n) { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed % n; };
var bad = 0;
var langs = ['js', 'bash', 'txt'];
for (var i = 0; i < 20000; i++) {
  var s = '';
  for (var k = 0, n = 3 + rnd(12); k < n; k++) s += toks[rnd(toks.length)];
  for (var l = 0; l < langs.length; l++) {
    if (/<[a-zA-Z][^>]*\son[a-zA-Z]+\s*=/.test(window.primerVendor.highlightCode(s, langs[l]))) bad++;
  }
}
bad;
"""
    assert ctx.eval(js) == 0


# ---------------------------------------------------------------------------
# UI-H3: window.renderMarkdown returns React children, not an HTML string.
# Passing that array to dangerouslySetInnerHTML's __html stringifies it as
# "[object Object],[object Object]", which is what a markdown document
# used to render as in the collections read-only pane.
# ---------------------------------------------------------------------------

KNOWLEDGE = UI / "components" / "knowledge.jsx"


def test_no_jsx_passes_render_markdown_through_inner_html() -> None:
    for path in sorted(UI.rglob("*.jsx")):
        assert "__html: window.renderMarkdown" not in path.read_text(encoding="utf-8"), (
            f"{path.name} passes renderMarkdown's React children through __html"
        )


_KN_PRELUDE = """
var __doc = { body: "# T\\n\\nx", document: { title: "Doc" }, children: [] };
window.location = { origin: "http://x", search: "", hash: "", pathname: "/console/" };
globalThis.Btn = function (props) { return React.createElement("div", { "data-testid": props["data-testid"] }); };
globalThis.Icon = function () { return null; };
window.primerApi = {
  apiFetch: function () { return Promise.resolve({}); },
  useResource: function () { return { data: __doc, error: null, refetch: function () {} }; },
};
"""


def _doc_pane_context():
    from tests.ui._mini_react import mini_react_context, transpile

    prelude = (
        _KN_PRELUDE
        + HIGHLIGHT_JSON.read_text(encoding="utf-8")
        + "\n"
        + HIGHLIGHT_PYTHON.read_text(encoding="utf-8")
        + "\n"
        + HIGHLIGHT_CODE.read_text(encoding="utf-8")
        + "\n"
        + transpile(MARKDOWN)
    )
    return mini_react_context(transpile(KNOWLEDGE), prelude)


def test_kn_document_pane_renders_markdown_as_elements() -> None:
    # The real renderMarkdown (vendor/markdown.jsx) runs in the context, so
    # the read-only branch must draw the heading as a real element: with
    # the old __html wiring the elements sat in a prop and no heading
    # element existed for a screen reader or a test to find.
    ctx = _doc_pane_context()
    # Wrap the component so the test can read its rendered output (the
    # harness keeps the root element in a closure; the wrapper hands its
    # return value back, which the harness renders in place).
    ctx.eval(
        "var __knOut = null; var __knOrig = KN_DocumentPane;"
        "KN_DocumentPane = function (props) { __knOut = __knOrig(props); return __knOut; };"
    )
    ctx.eval(
        'MR.mount(KN_DocumentPane, { collection: { id: "c1" }, path: "docs/readme", '
        "readOnly: true, pushToast: function () {}, onChanged: function () {}, onSelect: function () {} })"
    )
    types = ctx.eval(
        "(function () { var out = [];"
        " (function w(n) {"
        "  if (n == null || typeof n !== 'object') return;"
        "  if (Array.isArray(n)) { n.forEach(w); return; }"
        "  if (!n.__el) return;"
        "  if (typeof n.type === 'string') out.push(n.type);"
        "  w(typeof n.type === 'function' && n.type !== React.Fragment ? n.out : n.children);"
        " })(__knOut); return out; })()"
    )
    assert "h1" in types, "the '# T' heading must render as a real element"
    assert "p" in types, "the paragraph must render as a real element"
    texts = ctx.eval("MR.texts()")
    assert "T" in texts and "x" in texts
    assert "[object Object]" not in " ".join(texts)
