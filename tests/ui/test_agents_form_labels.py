"""The agent form names the rows that hold a custom widget, and the managed agent's summary is a definition list (console review C-003, the agents surface).

``AG_NewAgentModal`` had already given its plain inputs an ``id`` and a ``htmlFor``; three rows were left as a bare ``<label className="field-label">``: the model-profile
picker (a column of buttons), the system-prompt parts (a list of textareas) and the tool picker. None of them names its widget now: each row is a ``FormField`` (a
``role="group"`` named by its label, since no native control is its direct child), and every system-prompt part carries its own ``aria-label``. The locked summary of a
harness-managed agent drew ``<span className="field-label">Name</span><div>value</div>`` four times: captions for read-only values, with nothing to label; it is a
``<dl>`` now (``AG_ManagedSummary``).
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "agents.jsx").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = SRC.index("function " + name + "(")
    return SRC[start:SRC.index("\n}\n", start) + len("\n}\n")]


def test_the_three_custom_widget_rows_are_form_fields() -> None:
    assert '<FormField label="Model profile" hint="default, overridable per run">' in SRC
    assert '<FormField label="System prompt" hint="optional · parts">' in SRC
    assert re.search(r'<FormField label="Tools" hint=\{"scoped ids \\u2014 never whole toolsets"\}>\s*<window\.ToolPicker', SRC)
    assert 'id="na-model-profile-label"' not in SRC, "the hand-made label id is the FormField's now"


def test_each_system_prompt_part_is_named() -> None:
    block = SRC[SRC.index("systemPromptParts.map("):SRC.index('data-testid="agent-system-prompt-add"')]
    assert re.search(r"<textarea[^>]*aria-label=\{`System prompt part \$\{i \+ 1\}`\}", block, re.S), block[:600]


def test_the_form_declares_the_row_it_uses() -> None:
    assert re.match(r"/\* global [^*]*\bFormField\b", SRC)


# ---- the managed agent's locked summary ---------------------------------------------------------------------------------------------------------------

_PRELUDE = r"""
var ELS = [];
var __ce = React.createElement;
React.createElement = function (type, props) {
  var el = __ce.apply(null, arguments);
  if (typeof type === "string") ELS.push({ type: type, props: el.props });
  return el;
};
function __text(n) {
  if (n == null || typeof n === "boolean") return "";
  if (typeof n === "string" || typeof n === "number") return String(n);
  if (Array.isArray(n)) return n.map(__text).join("");
  return n.props ? __text(n.props.children) : "";
}
function __view() { return JSON.stringify(ELS.map(function (e) { return { type: e.type, className: e.props.className || "", text: __text(e.props.children) }; })); }
"""


@functools.lru_cache(maxsize=1)
def _compiled() -> str:
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    bundler = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    try:
        return bundler._transform(_function("AG_ManagedSummary"), "snippet.jsx")
    finally:
        bundler._ctx.close()


@pytest.fixture
def summary():
    made = []

    def go(existing: dict) -> list[dict]:
        ctx = mini_react_context(_compiled(), _PRELUDE)
        made.append(ctx)
        ctx.eval(f"function Host() {{ return React.createElement(AG_ManagedSummary, {{ existing: {json.dumps(existing)} }}); }} MR.mount(Host, {{}}); ELS.length = 0; MR.rerender();")
        return json.loads(ctx.eval("__view()"))

    try:
        yield go
    finally:
        for c in made:
            c.close()


_AGENT = {"id": "refund-triage", "description": "Triages refunds", "model": {"profile_id": "fast"}, "tools": ["a", "b", "c"]}


def test_the_summary_is_a_definition_list_of_four_terms(summary) -> None:
    view = summary(_AGENT)
    assert [e["type"] for e in view].count("dl") == 1
    terms = [(e["text"], e["className"]) for e in view if e["type"] == "dt"]
    assert terms == [("Name", "field-label"), ("Description", "field-label"), ("Model profile", "field-label"), ("Tools", "field-label")]
    assert [e["text"] for e in view if e["type"] == "dd"] == ["refund-triage", "Triages refunds", "fast", "3 registered"]


def test_no_caption_is_a_span_or_div_styled_as_a_label(summary) -> None:
    assert [e for e in summary(_AGENT) if e["type"] in ("span", "div") and "field-label" in e["className"].split()] == []


def test_a_missing_profile_reads_as_a_dash_and_no_tools_as_zero(summary) -> None:
    view = summary({"id": "x", "description": "d", "tools": []})
    assert [e["text"] for e in view if e["type"] == "dd"] == ["x", "d", "\u2014", "0 registered"]
