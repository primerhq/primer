"""When a graph binding is selected and the graph's Begin has input_schema,
the shared new-session form renders a dynamic schema-driven form. Without
input_schema, the free-text instructions textarea is preserved.

The form + submit logic was unified into ui/components/new-session-form.jsx
(FD2); the old NewSessionModal (app.jsx) is now a thin wrapper that renders
window.SharedNewSessionForm, so this schema behavior is asserted there."""

from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "new-session-form.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_modal_reads_begin_input_schema() -> None:
    src = _src()
    assert "input_schema" in src and "begin" in src.lower()


def test_modal_packages_into_graph_input_field() -> None:
    assert "graph_input" in _src()


def test_modal_falls_back_to_textarea_without_schema() -> None:
    src = _src()
    assert "initial_instructions" in src


# ---------------------------------------------------------------------------
# studio-ux fix 6: a graph's dynamic Begin.input_schema string field (e.g. a
# real-world "question" property declared as `{"type": "string"}` with no
# maxLength — see tests/ui_e2e/test_graph_builder_feedback_loop.py) used to
# render as a cramped single-line <input>. Plain string fields now default to
# a resizable multi-line <textarea>; only an EXPLICIT short maxLength keeps a
# field single-line.
# ---------------------------------------------------------------------------


def _schema_field_fn_src() -> str:
    src = _src()
    start = src.index("function SharedNewSessionSchemaField(")
    end = src.index("// Read a File as raw base64")
    return src[start:end]


def test_plain_string_field_without_maxlength_defaults_to_a_textarea() -> None:
    fn = _schema_field_fn_src()
    assert (
        "var long = !schema || typeof schema.maxLength !== \"number\" || schema.maxLength >= 120;"
        in fn
    )


def test_long_branch_still_renders_the_resizable_textarea() -> None:
    fn = _schema_field_fn_src()
    # The (renamed-in-spirit, same-shaped) "long" branch is unchanged — a
    # plain textarea using the shared .textarea class, which already carries
    # resize:vertical (ui/styles.css), so a "question"-like field is both
    # multi-line AND resizable out of the box.
    assert 'control = long ? (\n      <textarea' in fn
    assert 'className="textarea"' in fn
    assert "rows={4}" in fn


def test_a_short_explicit_maxlength_still_renders_a_single_line_input() -> None:
    # An author who WANTS a short single-line field (e.g. a "name"/"id"
    # property) signals it with an explicit small maxLength; that path is
    # preserved as the `<input type="text">` branch.
    fn = _schema_field_fn_src()
    assert '<input\n        type="text"' in fn
