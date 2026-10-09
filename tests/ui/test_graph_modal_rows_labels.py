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
    select = NEW_GRAPH[NEW_GRAPH.index("<select"):]
    select = select[:select.index(">")]
    assert 'aria-label="Seed agent"' in select


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
    assert 'data-testid="graph-import-spec"' in IMPORT_SPEC, "the journeys address the textarea by this id"


# ---------------------------------------------------------------------------
# GR_JsonField, in V8
# ---------------------------------------------------------------------------

_EXTRA = r"""
function __view2() {
  return JSON.stringify(ELS.map(function (e) {
    var p = e.props;
    return { type: e.type, id: p.id, htmlFor: p.htmlFor, role: p.role, ariaLabel: p["aria-label"], className: p.className, describedBy: p["aria-describedby"], text: __text(p.children) };
  }));
}
"""


@functools.lru_cache(maxsize=1)
def _compiled() -> str:
    from primer.api._jsx_bundle import JSXBundler

    bundler = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    try:
        return bundler._transform("\n".join([_fn(FORM, "FF_fieldControls"), _fn(FORM, "FormField"), _fn(GRAPHS, "GR_JsonField")]), "snippet.jsx")
    finally:
        bundler._ctx.close()


@pytest.fixture
def json_field():
    made: list = []

    def go(props: dict) -> list[dict]:
        ctx = mini_react_context(_compiled(), _PRELUDE + _EXTRA)
        made.append(ctx)
        ctx.eval(f"function Host() {{ return React.createElement(GR_JsonField, {json.dumps(props)}); }} MR.mount(Host, {{}}); ELS.length = 0; MR.rerender();")
        return [{k: e.get(k) for k in ("type", "id", "htmlFor", "role", "ariaLabel", "className", "describedBy", "text")} for e in json.loads(ctx.eval("__view2()"))]

    try:
        yield go
    finally:
        for c in made:
            c.close()


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


def test_the_builders_schema_view_passes_the_name(json_field) -> None:
    call = SCHEMA[SCHEMA.index("<GR_JsonField"):]
    call = call[:call.index("/>")]
    assert 'ariaLabel="Schema as JSON"' in call
