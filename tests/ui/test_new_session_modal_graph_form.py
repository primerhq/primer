"""The graph-input row of the New session overlay (``SharedNewSessionSchemaField``, ui/components/new-session-form.jsx).

When a graph's Begin declares an object input_schema the overlay draws one of these rows per property. The create form that once held them was removed (see
tests/ui/test_retired_new_session_form.py); these pins are the helper's own."""

from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "new-session-form.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


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
    return src[start:src.index("\n}\n", start) + len("\n}\n")]


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
