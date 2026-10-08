"""The form-label RATCHET: a ``<label className="field-label">`` with no ``htmlFor`` names nothing, and the count of them only goes down (console review C-003).

A hand-drawn label is a SIBLING of its input: ``input.labels`` is empty, clicking the visible text focuses nothing and a screen reader lands on an unnamed edit field. ``FormField``
(``ui/components/shared/form-field.jsx``) ties the label to its control, and every form row converts to it, surface by surface. ``scripts/audit_field_labels.py`` counts the labels that
still name nothing per file; ``tests/ui/field_label_baseline.json`` records the count. A file may not have MORE than its baseline (a new bare label fails: use ``FormField``), and may not
have FEWER either: the baseline goes down with the conversion, so the gain cannot be given back later by a new bare label in the same file.

The scanner is checked on synthetic JSX first, so a change to it cannot quietly make the guard count nothing.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "audit_field_labels.py"
BASELINE = ROOT / "tests" / "ui" / "field_label_baseline.json"
UI = ROOT / "ui"


def _audit():
    spec = importlib.util.spec_from_file_location("audit_field_labels", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit():
    return _audit()


# ---- the scanner -----------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("jsx", "expected"),
    [
        ('<label className="field-label">Name</label>', 1),
        ('<label className="field-label" htmlFor="x">Name</label>', 0),
        ('<label htmlFor="x" className="field-label">Name</label>', 0),
        ('<label className="field-label" htmlFor={id}>Name</label>', 0),
        ('<label\n  className="field-label"\n  style={{ display: "flex" }}\n>Name</label>', 1),
        ('<label\n  className="field-label"\n  htmlFor={wired.found ? wired.controlId : undefined}\n>Name</label>', 0),
        # a ``>`` inside an expression or a string does not end the tag, so a later ``htmlFor`` is still seen
        ('<label className={a > b ? "field-label" : "x"} htmlFor="y">Name</label>', 0),
        ('<label title="a > b" className="field-label">Name</label>', 1),
        ('<label className="field-label" onClick={() => go(1)}>Name</label>', 1),
        # another kind of label is not a form row's
        ('<label className="chip">Name</label>', 0),
        ('<label>Name</label>', 0),
        ('<labels className="field-label">', 0),
        # a label that WRAPS its control names it implicitly
        ('<label className="field-label"><input type="checkbox" /> <span>auto</span></label>', 0),
        ('<label className="field-label">Pick <select><option>a</option></select></label>', 0),
        ('<label className="field-label"><textarea /></label>', 0),
        # a comment that starts a line may quote the pattern; code after it still counts
        ('// the old row: <label className="field-label">Name</label>\n<label className="field-label">X</label>', 1),
        ('/* <label className="field-label">Name</label>\n   <label className="field-label">Other</label> */\nconst x = 1;', 0),
        ('  // <label className="field-label">Name</label>', 0),
        # two in one file are two
        ('<label className="field-label">A</label><input/><label className="field-label">B</label><input/>', 2),
        ('<label className="field-label">A</label><label className="field-label" htmlFor="b">B</label>', 1),
    ],
)
def test_the_scanner_counts_the_labels_that_name_nothing(audit, jsx: str, expected: int) -> None:
    assert audit.count_bare_labels(jsx) == expected, jsx


def test_the_scanner_finds_labels_in_the_real_tree(audit) -> None:
    counts = audit.scan()
    assert sum(counts.values()) > 0 and all(rel.endswith(".jsx") for rel in counts), "a scan that finds nothing would pass the ratchet vacuously"


def test_the_scanner_skips_vendored_code(audit) -> None:
    assert not any(rel.startswith("vendor/") for rel in audit.scan())


# ---- the ratchet -------------------------------------------------------------------------------------------------------------------------------------


def test_the_baseline_is_a_per_file_count_of_whole_numbers() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert baseline and all(isinstance(n, int) and n > 0 for n in baseline.values()), "a file with none is left out of the baseline"
    assert all((UI / rel).is_file() for rel in baseline), "a baseline entry names a file that is gone: drop it"


def test_no_file_gets_a_new_bare_field_label(audit) -> None:
    """A new ``<label className="field-label">`` without ``htmlFor`` fails here: draw the row with ``FormField``."""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    grew = {rel: (baseline.get(rel, 0), n) for rel, n in audit.scan().items() if n > baseline.get(rel, 0)}
    assert not grew, f"bare field-label count went UP (file: baseline -> now); use FormField (ui/components/shared/form-field.jsx): {grew}"


def test_the_baseline_goes_down_with_every_conversion(audit) -> None:
    """A file with FEWER bare labels than its baseline fails until the baseline is lowered: ``uv run python scripts/audit_field_labels.py --write``."""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    now = audit.scan()
    fell = {rel: (n, now.get(rel, 0)) for rel, n in baseline.items() if now.get(rel, 0) < n}
    assert not fell, f"bare field-label count went DOWN (file: baseline -> now); lower tests/ui/field_label_baseline.json (scripts/audit_field_labels.py --write): {fell}"


def test_the_baseline_file_is_what_the_script_writes(audit) -> None:
    assert BASELINE.read_text(encoding="utf-8") == audit.render(audit.scan()), "run scripts/audit_field_labels.py --write"
