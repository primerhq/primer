"""The graph modals' rows, and the builder's JSON view, name their controls (console review C-003, the graphs surface).

``GR_NewGraphModal`` (ID, Description, Seed agent) and ``GR_ImportSpecModal`` (Graph spec JSON) drew each row as a ``<div className="field">`` holding a bare
``<label className="field-label">`` beside the control: ``input.labels`` was empty, the server's field error was a loose ``<div>``, and a click on the visible text focused nothing.
Each row is a ``FormField`` now. ``GR_JsonField`` is the textarea the builder shows when a schema is too complex for its field rows (``gb-schema.jsx`` calls it with ``label=""``): with no
label it had NO name at all; it takes an ``ariaLabel`` now and ``gb-schema.jsx`` passes ``"Schema as JSON"``.

``GR_JsonField`` runs in V8 on the hook runtime of ``tests/ui/_mini_react.py`` (the harness of ``test_form_rows_label_their_control.py``); the modals are pinned in the same slicing style as
the rest of ``tests/ui``, and in a browser by ``tests/ui_e2e/test_graph_modal_labels_journey.py``.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context
from tests.ui.test_form_rows_label_their_control import _PRELUDE, _fn

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
GRAPHS = (UI / "components" / "graphs.jsx").read_text(encoding="utf-8")
FORM = (UI / "components" / "shared" / "form-field.jsx").read_text(encoding="utf-8")
SCHEMA = (UI / "components" / "graph-builder" / "gb-schema.jsx").read_text(encoding="utf-8")


NEW_GRAPH = _fn(GRAPHS, "GR_NewGraphModal")
IMPORT_SPEC = _fn(GRAPHS, "GR_ImportSpecModal")


def _open_tag(src: str, pattern: str) -> str:
    """The whole opening tag that starts at the first match of ``pattern``: up to the first ``>`` outside braces and quotes (the ``=>`` of an attribute's arrow function does not end it)."""
    start = re.search(pattern, src).start()
    depth, quote = 0, None
    for i in range(start, len(src)):
        c = src[i]
        if quote:
            quote = None if c == quote else quote
        elif c in "\"'`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ">" and depth == 0:
            return src[start:i + 1]
    raise AssertionError(f"no end of the tag at {pattern!r}")


def test_open_tag_reads_past_an_arrow_function_and_a_quoted_angle_bracket() -> None:
    tag = _open_tag('<a x="1 > 2" onChange={(e) => go(e)} y="z">text</a>', r"<a ")
    assert tag == '<a x="1 > 2" onChange={(e) => go(e)} y="z">'


def test_the_page_declares_the_row_it_uses() -> None:
    assert "FormField" in GRAPHS.splitlines()[0], "the /* global */ line must name FormField"


# ---------------------------------------------------------------------------
# GR_NewGraphModal
# ---------------------------------------------------------------------------


def test_the_new_graph_modal_draws_id_description_and_seed_agent_as_form_fields() -> None:
    for label in ("ID", "Description", "Seed agent"):
        assert re.search(rf'<FormField\s+label="{label}"', NEW_GRAPH), f"the {label} row is not a FormField"
    assert "field-label" not in NEW_GRAPH, "a bare label is left in the modal"


def test_the_server_errors_are_the_rows_err_and_not_a_loose_div() -> None:
    assert 'err={fieldErrors["body.id"]}' in NEW_GRAPH
    assert 'err={fieldErrors["body.description"]}' in NEW_GRAPH
    assert 'fieldErrors["body.id"] && (' not in NEW_GRAPH and 'fieldErrors["body.description"] && (' not in NEW_GRAPH


def test_the_seed_select_is_named_itself_because_its_row_holds_it_inside_a_wrapper() -> None:
    """The select sits next to the New button in a flex wrapper, so the row is a group; the select keeps its own name."""
    assert 'aria-label="Seed agent"' in _open_tag(NEW_GRAPH, r"<select\b")


def test_the_seed_rows_explanation_is_the_rows_help_so_the_group_is_described_by_it() -> None:
    """A loose ``field-help`` child names nothing; the ``help`` prop gives the line an id and the row (a group here) ``aria-describedby``. Only the amber warning is left as a child."""
    assert "help={" in _open_tag(NEW_GRAPH, r'<FormField\s+label="Seed agent"')
    loose = re.findall(r'<div className="field-help"[^>]*>', NEW_GRAPH)
    assert all("--amber" in tag for tag in loose) and len(loose) == 1, loose


def test_the_hints_are_written_with_a_dash_escape_not_a_literal_dash() -> None:
    """A JSX attribute string does not process escapes, so the hint is an expression: ``hint={"... \\u2014 ..."}``."""
    assert 'hint={"optional \\u2014 backend assigns if blank"}' in NEW_GRAPH


# ---------------------------------------------------------------------------
# GR_ImportSpecModal
# ---------------------------------------------------------------------------


def test_the_import_spec_modal_draws_its_json_as_a_form_field_with_the_error_as_err() -> None:
    assert re.search(r'<FormField\s+label="Graph spec JSON"', IMPORT_SPEC)
    assert "err={error}" in IMPORT_SPEC
    assert "field-label" not in IMPORT_SPEC
    assert "help={" in _open_tag(IMPORT_SPEC, r'<FormField\s+label="Graph spec JSON"')
    assert "field-help" not in IMPORT_SPEC, "the explanation is the row's help, not a loose line"
    assert 'data-testid="graph-import-spec"' in IMPORT_SPEC, "the journeys address the textarea by this id"


# ---------------------------------------------------------------------------
# GR_JsonField, in V8
# ---------------------------------------------------------------------------

_EXTRA = r"""
function __view2() {
  return JSON.stringify(ELS.map(function (e) {
    var p = e.props;
    return { type: e.type, id: p.id, htmlFor: p.htmlFor, role: p.role, ariaLabel: p["aria-label"], className: p.className, describedBy: p["aria-describedby"], invalid: p["aria-invalid"], text: __text(p.children) };
  }));
}
function Modal(props) { return React.createElement("div", null, props.children, props.footer); }
function Btn(props) { return React.createElement("button", { onClick: props.onClick }, props.children); }
// type into the first textarea, then blur it (the field parses on blur); each step reads the latest render's handlers
function __typeAndBlur(text) {
  function area() { ELS.length = 0; MR.rerender(); return ELS.filter(function (e) { return e.type === "textarea"; })[0]; }
  area().props.onChange({ target: { value: text } });
  area().props.onBlur();
  ELS.length = 0; MR.rerender();
}
"""


@functools.lru_cache(maxsize=1)
def _compiled() -> str:
    from primer.api._jsx_bundle import JSXBundler

    bundler = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    try:
        return bundler._transform("\n".join([_fn(FORM, "FF_fieldControls"), _fn(FORM, "FormField"), _fn(GRAPHS, "GR_JsonField"), _fn(GRAPHS, "GR_ImportSpecModal")]), "snippet.jsx")
    finally:
        bundler._ctx.close()


@pytest.fixture
def mounter():
    made: list = []

    def go(component: str, props: dict, type_text: str | None = None) -> list[dict]:
        ctx = mini_react_context(_compiled(), _PRELUDE + _EXTRA)
        made.append(ctx)
        ctx.eval(f"function Host() {{ return React.createElement({component}, {json.dumps(props)}); }} MR.mount(Host, {{}}); ELS.length = 0; MR.rerender();")
        if type_text is not None:
            ctx.eval(f"__typeAndBlur({json.dumps(type_text)})")
        keys = ("type", "id", "htmlFor", "role", "ariaLabel", "className", "describedBy", "invalid", "text")
        return [{k: e.get(k) for k in keys} for e in json.loads(ctx.eval("__view2()"))]

    try:
        yield go
    finally:
        for c in made:
            c.close()


@pytest.fixture
def json_field(mounter):
    return lambda props, type_text=None: mounter("GR_JsonField", props, type_text)


@pytest.fixture
def import_modal(mounter):
    return lambda props: mounter("GR_ImportSpecModal", props)


def _one(view: list[dict], type_: str) -> dict:
    found = [e for e in view if e["type"] == type_]
    assert len(found) == 1, (type_, view)
    return found[0]


def test_a_labelled_json_field_points_its_label_at_the_textarea(json_field) -> None:
    view = json_field({"label": "response_format", "value": {"type": "object"}})
    label, textarea = _one(view, "label"), _one(view, "textarea")
    assert textarea["id"] and label["htmlFor"] == textarea["id"] and label["text"].startswith("response_format"), view


def test_a_json_field_with_no_label_draws_no_label_and_names_the_textarea_itself(json_field) -> None:
    view = json_field({"label": "", "value": {"type": "object"}, "ariaLabel": "Schema as JSON"})
    assert [e for e in view if e["type"] == "label"] == [], "an empty label element names nothing"
    assert _one(view, "textarea")["ariaLabel"] == "Schema as JSON"


def test_a_json_field_with_neither_label_nor_name_still_gets_a_default_name(json_field) -> None:
    view = json_field({"label": "", "value": None})
    assert _one(view, "textarea")["ariaLabel"] == "JSON"


def test_a_labelled_json_field_does_not_also_carry_an_aria_label(json_field) -> None:
    view = json_field({"label": "output_schema", "value": None, "ariaLabel": "ignored"})
    assert _one(view, "textarea")["ariaLabel"] is None


def test_the_help_line_is_still_drawn(json_field) -> None:
    view = json_field({"label": "", "value": None, "help": "Named fields here become chips.", "ariaLabel": "x"})
    assert any(e["text"] == "Named fields here become chips." for e in view), view


def test_the_builders_schema_view_passes_the_name() -> None:
    call = SCHEMA[SCHEMA.index("<GR_JsonField"):]
    call = call[:call.index("/>")]
    assert 'ariaLabel="Schema as JSON"' in call


def test_the_help_line_is_what_describes_the_textarea(json_field) -> None:
    """The live branch (the builder passes no label): the help line has an id and the textarea names it in ``aria-describedby``."""
    view = json_field({"label": "", "value": None, "help": "Named fields here become chips.", "ariaLabel": "x"})
    help_line = next(e for e in view if e["text"] == "Named fields here become chips.")
    assert help_line["id"], "the help line has no id to be described by"
    assert _one(view, "textarea")["describedBy"] == help_line["id"], view


def test_a_field_with_no_help_and_no_error_is_neither_described_nor_invalid(json_field) -> None:
    textarea = _one(json_field({"label": "", "value": None, "ariaLabel": "x"}), "textarea")
    assert textarea["describedBy"] is None and textarea["invalid"] is None
    assert [e for e in json_field({"label": "", "value": None, "ariaLabel": "x"}) if e["role"] == "alert"] == []


def test_a_parse_error_is_an_alert_the_invalid_textarea_is_described_by(json_field) -> None:
    view = json_field({"label": "", "value": None, "help": "Named fields here become chips.", "ariaLabel": "x"}, type_text="{")
    alert = next((e for e in view if e["role"] == "alert"), None)
    assert alert is not None, ("a parse error is drawn as a plain div", view)
    assert alert["id"] and alert["text"].startswith("JSON parse:"), alert
    textarea = _one(view, "textarea")
    assert textarea["invalid"] == "true", textarea
    help_line = next(e for e in view if e["text"] == "Named fields here become chips.")
    assert textarea["describedBy"].split() == [help_line["id"], alert["id"]], (textarea, help_line, alert)


def test_the_import_modals_help_line_is_the_textareas_description(import_modal) -> None:
    view = import_modal({"currentDraft": None})
    help_line = next(e for e in view if e["text"].startswith("Loads the pasted spec into the visual editor"))
    textarea = _one(view, "textarea")
    assert help_line["id"] and textarea["describedBy"] == help_line["id"], (textarea, help_line)
