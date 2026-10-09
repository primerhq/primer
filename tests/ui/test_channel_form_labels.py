"""The channel provider and channel forms draw every field as a ``FormField`` (console review C-003, the channels surface).

Both modals (``ChannelProviderModal``-style and ``NewChannelModal``, ``ui/components/channels.jsx``) drew each row as ``<div className="field"><label className="field-label">...`` with
the label as a SIBLING of the control and the server's field error as a loose ``<div className="field-help">``. Each row is a ``FormField`` now: the label points at the control, the
server's message is the row's ``err`` (announced, tied to the control by ``aria-invalid`` / ``aria-describedby``), and the external-id format note is its ``help``.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "channels.jsx").read_text(encoding="utf-8")


def test_no_bare_field_label_is_left() -> None:
    assert '<label className="field-label">' not in SRC


def test_both_forms_draw_eight_rows_as_form_fields() -> None:
    """The provider form: id, platform, and one row per config field (a map); the channel form: id, provider, external id, label, relay mode."""
    assert SRC.count("<FormField ") == 8
    for label in ('label="id"', 'label="platform"', 'label="provider"', 'label="external id"', 'label="label"', 'label="relay mode"', "label={f.label}"):
        assert label in SRC, label


def test_the_server_errors_are_each_rows_err_and_not_a_loose_div() -> None:
    assert not re.search(r'fieldErrors\["body\.[a-z_]+"\] && <div className="field-help"', SRC)
    for key in ("body.id", "body.provider", "body.provider_id", "body.external_id", "body.label"):
        assert f'err={{fieldErrors["{key}"]}}' in SRC, key
    assert "err={err}" in SRC, "the config-field rows' own error"


def _attr(text: str) -> bool:
    """``text`` appears as a prop of its own: after whitespace, so ``data-help=`` or ``ariahint=`` do not count."""
    return re.search(r"(?<=\s)" + re.escape(text), SRC) is not None


def test_the_external_id_format_note_is_the_rows_help() -> None:
    assert _attr('help="Slack: channel ID · Telegram: chat ID · Discord: snowflake"')


def test_the_attribute_pin_refuses_a_lookalike_prop() -> None:
    assert re.search(r"(?<=\s)" + re.escape('help="x"'), 'a data-help="x"') is None
    assert re.search(r"(?<=\s)" + re.escape('help="x"'), '<FormField help="x"') is not None


def test_the_locked_hints_are_written_without_a_literal_dash() -> None:
    assert _attr('hint={isEdit ? "locked \\u2014 id cannot change after create" : "auto-generated if blank"}')
    assert _attr('hint={isEdit ? "locked \\u2014 recreate to change platform" : undefined}')
    assert _attr('hint={isEdit ? "locked \\u2014 recreate to change provider" : undefined}')


def test_the_page_declares_the_row_it_uses() -> None:
    assert re.match(r"/\* global [^*]*\bFormField\b", SRC)


# ---------------------------------------------------------------------------
# the platform config fields (follow-ups of the #634 review)
# ---------------------------------------------------------------------------


def test_each_config_fields_hint_is_the_rows_help_so_the_input_is_described_by_it() -> None:
    """A loose ``<div className="field-help">`` is not in ``aria-describedby``; a checkbox field keeps its hint as the box's own words."""
    assert _attr('help={f.type === "checkbox" ? undefined : f.hint}')
    assert '{f.hint && f.type !== "checkbox" && <div className="field-help">{f.hint}</div>}' not in SRC


def test_the_required_marker_is_the_rows_hint_and_the_input_says_aria_required() -> None:
    """The amber ``required`` word is part of the label's name ("Bot token required"); nothing else pinned it, so ``hint={undefined}`` passed every test."""
    assert _attr('hint={f.required ? <span style={{ color: "var(--amber)" }}>required</span> : undefined}')
    assert SRC.count('aria-required={f.required ? "true" : undefined}') == 3, "the checkbox, number and text inputs of a config field"
