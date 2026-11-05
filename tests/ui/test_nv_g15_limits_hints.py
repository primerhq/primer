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


# ---- G15R-02 + G15R-03: the LIVE limits surface and the share-dialog grant levels ---------------------------------
#
# G15-06's hint landed only in the dead no-frame fallback above. The live
# console surface is PC_LimitsSection (ui/components/providers/
# register-common.jsx), whose help is the PC_LIMIT_HELP map: a blank
# connect_timeout_seconds writes null (no bound) for the LLM/embedding
# classes, and with the shipped single slot (max_concurrency 1) one
# black-holing connect holds the only slot unbounded (primer/llm/
# _timeout.py). G15R-03: the share dialog's grant-level select (ui/
# components/shared/share-dialog.jsx) offered bare read/full with the
# semantics only in docs/dev/architecture/access-control.md:10/:142.
# Both fixes are COPY ONLY. Polarity: RED at the base (the consequence
# and the level semantics are absent), GREEN once the copy lands.

LIVE_SRC = (ROOT / "ui" / "components" / "providers" / "register-common.jsx").read_text(encoding="utf-8")
SHARE_SRC = (ROOT / "ui" / "components" / "shared" / "share-dialog.jsx").read_text(encoding="utf-8")

LIVE_CONNECT_HELP = (
    "Empty means no connect limit; with one concurrency slot, a black-holing "
    "connect holds the only slot indefinitely - set a value to fail fast."
)

GRANT_READ_OPTION = "read: can view and use it"
GRANT_FULL_OPTION = "full: can also edit it; only owners can delete it or change sharing"


def _limit_help_block() -> str:
    """The PC_LIMIT_HELP map source (the live surface's help strings)."""
    i = LIVE_SRC.index("const PC_LIMIT_HELP")
    return LIVE_SRC[i : LIVE_SRC.index("};", i)]


def _grant_select_body() -> str:
    """The grant-level <select>, from its testid to the closing tag."""
    i = SHARE_SRC.index('shr-grant-level-')
    j = SHARE_SRC.index("</select>", i)
    return SHARE_SRC[i : j + len("</select>")]


def test_live_connect_help_carries_the_single_slot_consequence():
    block = _limit_help_block()
    assert f'connect_timeout_seconds: "{LIVE_CONNECT_HELP}",' in block, (
        "G15R-02: the live limits surface (PC_LIMIT_HELP) never says the "
        "single-slot queue-hold consequence of a blank connect timeout - "
        "the #1005 hint sits in the dead no-frame fallback only"
    )
    # The request line keeps its correct live copy (blank writes null = no
    # stall timeout); the two surfaces' blank semantics differ, so the
    # fallback hint must not have been copied here.
    assert 'request_timeout_seconds: "Empty means no stall timeout"' in block


def test_share_dialog_grant_levels_carry_their_semantics():
    select = _grant_select_body()
    assert f'<option value="read">{GRANT_READ_OPTION}</option>' in select, (
        "G15R-03: the share dialog offers bare 'read' with no semantics"
    )
    assert f'<option value="full">{GRANT_FULL_OPTION}</option>' in select, (
        "G15R-03: the share dialog offers bare 'full' with no semantics"
    )
