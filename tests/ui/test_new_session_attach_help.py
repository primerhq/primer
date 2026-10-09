"""The Create session overlay says where files go (ticket 01a11dd7-fc79, the lead's option B).

The overlay has no file picker and the create call takes no attachments: ``attachments`` is a field of the STEER body and needs an ``instruction`` to ride in on. Someone who wants to give
a new session a file found no way and no explanation. The ``Initial instructions`` field now carries a help line that says it: create the session parked, then attach the files to the
first message in the session's composer.

``NV_Field``'s help line (drawn, and listed in the control's ``aria-describedby``) is pinned in ``test_form_rows_label_their_control.py``; what is pinned here is that the overlay passes
THIS sentence to it, and that the sentence is not repeated on the other fields. The sentence is read out of the overlay's source, so the V8 mount draws exactly what the overlay passes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.ui.test_form_rows_label_their_control import _fixture_for

ROOT = Path(__file__).resolve().parents[2]
OVERLAYS = (ROOT / "ui" / "components" / "console" / "nv-overlays.jsx").read_text(encoding="utf-8")

SENTENCE = "To attach files, create the session parked and attach them to your first message."


@pytest.fixture
def nv_field():
    yield from _fixture_for("NV_Field")


def _instructions_help() -> str | None:
    tag = re.search(r'<NV_Field label="Initial instructions"([^>]*)>', OVERLAYS)
    assert tag, "the Initial instructions field moved: update this pin"
    found = re.search(r'\bhelp="([^"]*)"', tag.group(1))
    return found.group(1) if found else None


def test_the_instructions_field_passes_the_attach_sentence_as_its_help() -> None:
    assert _instructions_help() == SENTENCE


def test_no_other_field_of_the_overlay_repeats_the_sentence() -> None:
    assert OVERLAYS.count(SENTENCE) == 1


def test_the_sentence_names_the_park_the_overlay_really_offers() -> None:
    """A help line that names a control the overlay does not have is worse than none: ``Create parked`` is the Autonomy switch under Advanced."""
    assert ">Create parked</button>" in OVERLAYS
    assert "parked" in SENTENCE


def test_the_help_line_is_drawn_under_the_field_and_describes_its_textarea(nv_field) -> None:
    sentence = _instructions_help()
    assert sentence, "the overlay passes no help to the Initial instructions field"
    view = nv_field("React.createElement('textarea', { className: 'nv-textarea' })", {"label": "Initial instructions", "help": sentence})
    textarea = [e for e in view if e["type"] == "textarea"]
    assert len(textarea) == 1 and textarea[0]["describedBy"], view
    described = [e for e in view if e["id"] == textarea[0]["describedBy"].split()[0]]
    assert len(described) == 1 and described[0]["text"] == sentence, view
