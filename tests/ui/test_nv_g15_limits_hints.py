"""G15-06 (LOW): the console Limits form must say what a blank timeout box means.

Blank ``connect_timeout_seconds`` means NO connect bound (the shipped
default), and the adapters acquire the provider's concurrency slot
before the open - so with the console's shipped single slot
(max_concurrency 1) one black-holing connect holds the only slot and
every later request for that provider queues behind it. Blank
``request_timeout_seconds`` means the 300 s per-event stall timeout
(default) that only starts once the stream is open. The trade-off lived
only in the model's field description (the strength - it stays); the
point of choice, the form's two timeout fields, said nothing.

The fix is COPY ONLY in ``PC_LimitsFieldset`` (ui/components/provider-form.jsx):
a one-line ``field-help`` hint under each timeout field, in the same
style the form's other fields use (cf. PC_Field's ``field.help``
render). No behavior change.

Audit: /var/tmp/revamp/audit/G15.md (G15-06); the probe
probe_g15_06_limits_form_blank_semantics.py pins the RED state.
Polarity: RED at the base commit (the texts are absent), GREEN once the
hints land.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "provider-form.jsx").read_text(encoding="utf-8")

CONNECT_HINT = (
    "Blank: no connect bound - a black-holing upstream holds the "
    "concurrency slot indefinitely; set a value to fail fast."
)
REQUEST_HINT = "Blank: 300 s per-event stall timeout."


def _fieldset_body() -> str:
    """The PC_LimitsFieldset source from its def to its closing
    line-start brace (the house slice, cf.
    test_nv_g14_refusal_copy._helper_body)."""
    i = SRC.index("function PC_LimitsFieldset")
    return SRC[i : SRC.index("\n}", i)]


def _label_body(fieldset: str, main_label: str) -> str:
    """The source of one <label>, from its main-label span to the
    closing </label>."""
    i = fieldset.index('pc-field-label-main">' + main_label + "</span>")
    j = fieldset.index("</label>", i)
    return fieldset[i : j + len("</label>")]


def test_connect_field_carries_the_blank_semantics_hint():
    fieldset = _fieldset_body()
    label = _label_body(fieldset, "Connect timeout (s)")
    assert f'<span className="field-help">{CONNECT_HINT}</span>' in label, (
        "G15-06: the connect timeout field has no hint - a blank box means "
        "no connect bound, and the single-slot queue-hold trade-off is "
        "invisible at the point of choice"
    )
    # The hint renders UNDER the field: after the input, inside the label.
    assert label.index(CONNECT_HINT) > label.index("<input")


def test_request_field_carries_the_blank_semantics_hint():
    fieldset = _fieldset_body()
    label = _label_body(fieldset, "Request timeout (s)")
    assert f'<span className="field-help">{REQUEST_HINT}</span>' in label, (
        "G15-06: the request timeout field has no hint - a blank box "
        "means the 300 s per-event stall timeout, and the form never says so"
    )
    assert label.index(REQUEST_HINT) > label.index("<input")
