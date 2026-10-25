"""The New rule dialog's selects carry explicit label associations, and no label wraps a select (round 2 of #932, CI fix).

The journey (tests/ui_e2e/test_channel_rule_reply_target_journey.py) reaches the form's reply target with
``.modal.get_by_label("Reply target", exact=True)``. Playwright computes that label's text from the labelling
element's own text: when a <select> sits INSIDE a wrapping <label>, the label's text carries the select's option
text ("Reply targetdefaultsource_threadsource_roomnone (post nothing)"), so the exact match resolves to nothing
(pinned empirically against the installed Playwright/chromium: wrapping shape count 0, explicit htmlFor shape count 1;
.modal .first scoping was checked and is not the problem). The agent form's passing label journey uses the explicit
id + htmlFor shape, which is what the rule modal now uses for every select. The dialog itself carries a stable
testid, so a test never depends on which .modal happens to be first in the DOM.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "ui" / "components" / "channel_rules.jsx"

# The explicit associations the modal's selects must carry: the label's htmlFor, and the select's own id.
SELECT_IDS = ("cr-provider", "cr-event-type", "cr-action", "cr-reply-target")


def _modal_source() -> str:
    """The CR_RuleModal function's source, from its def to the next top-level function (it is the file's last one)."""
    text = RULES.read_text(encoding="utf-8")
    start = text.index("function CR_RuleModal")
    end = text.find("\nfunction ", start + 1)
    return text[start:end if end != -1 else len(text)]


def test_the_rule_modal_carries_a_stable_testid() -> None:
    src = _modal_source()
    assert re.search(r"<Modal\s+title=\"New rule\"\s+testid=\"cr-rule-modal\"", src), (
        "the dialog must keep its stable testid: the journey and any follow-up reach it by name, not by .modal .first"
    )


def test_every_modal_select_has_an_explicit_label_association() -> None:
    src = _modal_source()
    for sid in SELECT_IDS:
        assert f"id=\"{sid}\"" in src, f"the select must carry its own id ({sid})"
        assert f"htmlFor=\"{sid}\"" in src, (
            f"a <label htmlFor> must name the select ({sid}): the label text then is the label alone, "
            "and get_by_label with exact matches it"
        )


def test_no_label_wraps_a_select() -> None:
    """The mechanism behind the CI failure: a <label> element whose text contains a <select>'s options breaks exact label matches."""
    text = RULES.read_text(encoding="utf-8")
    # Every <label ...> ... </label> span in the file (labels here are short, single-line openers or small wrappers).
    for m in re.finditer(r"<label\b[^>]*>[\s\S]*?</label>", text):
        assert "<select" not in m.group(0), (
            "a <select> inside a wrapping <label> puts the option text into the label's text and breaks "
            "get_by_label(exact=True) (and a screen reader's label): the select must sit beside its label, named by htmlFor"
        )
