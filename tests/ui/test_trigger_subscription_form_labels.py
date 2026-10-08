"""The trigger subscription dialog names its three non-input rows (console review C-003, the triggers surface).

``TR_SubscriptionDialog`` had given its inputs an ``id`` and a ``htmlFor`` by hand; three rows sat under a bare ``<label className="field-label">`` that named nothing: ``Kind``
(a group of radio buttons, a locked value in edit mode), ``Target`` (the locked target of an existing subscription, edit mode only) and ``Parallelism`` (two radios). Each is a
``FormField`` now: a ``role="group"`` named by its label, since no native control is a direct child.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "triggers.jsx").read_text(encoding="utf-8")


def test_the_kind_row_is_a_form_field_with_its_locked_hint() -> None:
    assert re.search(r'<FormField label="Kind" hint=\{isEdit \? "locked \\u2014 config is immutable" : undefined\}>\s*\{isEdit \? \(', SRC), "the Kind row"


def test_the_target_row_is_a_form_field() -> None:
    assert re.search(r'<FormField label="Target" hint="locked">\s*<div className="mono"[^>]*>\s*<TR_SubTargetLabel sub=\{initial\} />', SRC)


def test_the_parallelism_row_is_a_form_field_around_its_radios() -> None:
    block = SRC[SRC.index('<FormField label="Parallelism">'):]
    block = block[:block.index("</FormField>")]
    assert 'data-testid="tr-sub-parallelism"' in block and block.count('type="radio"') == 2


def test_no_bare_label_is_left_in_the_dialog() -> None:
    start = SRC.index("function TR_SubscriptionDialog(")
    dialog = SRC[start:SRC.index("\nwindow.TR_SubscriptionDialog", start)]
    assert not re.findall(r'<label className="field-label">', dialog)


def test_the_page_declares_the_row_it_uses() -> None:
    assert re.match(r"/\* global [^*]*\bFormField\b", SRC)
