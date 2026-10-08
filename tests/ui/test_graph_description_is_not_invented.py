"""The new graph form stops inventing a description (ticket 01a11cd1-6af6, the follow-up to ADM-14 of the 2026-10-08 admin review).

ADM-14 removed the ``(no description)`` placeholder from the AGENT form: what is sent is the trimmed description. The graph form was left behind: ``GR_NewGraphModal``
(``graphs.jsx``) still posted ``description || "(no description)"``, so a graph created with a blank description field stored a fake sentence. A graph description stays
OPTIONAL: the REST route accepts an empty one by design, and ``tests/e2e/test_compute_status.py::test_t0522_graph_with_empty_description_accepted`` pins that. So the form
now sends the trimmed description and, when the field is blank, sends ``""``; it must not start requiring one. The display-only consequences (a muted "No description" where
an empty description would render as nothing) live at the list and header sites, not in the submit.

The modal is JSX, so how it builds the body is a source check, in the same slicing style as ``tests/ui/test_agent_form_validation.py``.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "graphs.jsx").read_text(encoding="utf-8")


def _modal() -> str:
    start = SRC.index("function GR_NewGraphModal(")
    return SRC[start:SRC.index("function GraphsPage(", start)]


def test_the_submit_sends_the_trimmed_description_and_invents_no_placeholder() -> None:
    body = _modal()

    assert "description: description.trim()," in body
    assert '"(no description)"' not in body, "a blank description is sent as \"\", not replaced by a placeholder that is then stored"


def test_the_editor_input_placeholder_is_an_input_hint_that_stays_outside_the_modal() -> None:
    """The ticket keeps ``GR_GraphFields``'s placeholder: it is an input hint, not a submit value. The modal slice must stay placeholder-free for the check above to mean anything."""
    modal = _modal()
    fields = SRC[SRC.index("function GR_GraphFields("):]

    assert 'placeholder="(no description)"' in fields, "the editor input hint is untouched"
    assert 'placeholder="(no description)"' not in modal
