"""G13 red probes for ui/components/console/nv-palette.jsx.

G13-01 (MEDIUM): the 8 lazy result sources (sessions/recents/agents/
graphs/triggers/toolsets/files/wikiDocs) have no error surface - a
failed fetch degrades to a silent empty group, and when no other
group matches, the only copy is the wrong "Nothing matches 'q'."
answer even though a source failed to load.

G13-02 (LOW): the arrow-key selection is visual only (data-active):
the input is not a combobox, carries no aria-activedescendant, and
the rows have no role="option"/aria-selected - a screen reader hears
no signal about which row Enter will run.

These tests assert the POST-fix state: red at the red commit, green at
head (audit probes: /var/tmp/revamp/audit/G13/probe_g13_01_*.py and
probe_g13_02_*.py, both CONFIRMED at main by the lead). One guard
(recents' 404 special case, G13-01 spec item 3) is green before AND
after the fix by design.
"""
import json
import re

from tests.ui._register_harness import context as mr_context

JSX = "ui/components/console/nv-palette.jsx"


def _src():
    return open(JSX, encoding="utf-8").read()


def test_a_failed_source_is_named_and_a_retry_refires_it():
    """G13-01 spec 1: a failed source renders a per-source error state
    (what failed + a Retry that re-fires THAT source), not a silent
    empty group."""
    src = _src()
    assert "nv-palette-source-error" in src, "no per-source error block exists"
    assert "nv-palette-retry" in src, "no per-source Retry handle exists"
    i = src.index("nv-palette-source-error")
    region = src[i - 400 : i + 800]
    assert "Could not load" in region, "the error block does not name what failed"
    assert "refetch" in region, "the error block does not re-fire its source"


def test_the_source_error_predicate_evals_the_real_condition():
    """G13-01: the committed predicate for 'this source failed the read'
    must classify the resource states correctly (evaluated, not
    paraphrased): a failed read yes; a loaded-empty list, a still-
    loading resource, and a failed re-fetch over kept data are NOT
    error states (the data still renders)."""
    src = _src()
    m = re.search(r"function NV_sourceError\(res\) \{\n(.*?)\n\}", src, re.S)
    assert m, "no NV_sourceError predicate - a failed source has no error state"
    body = m.group(1)
    ctx = mr_context("function NV_sourceError(res) {\n" + body + "\n}")
    for name, shape, expected in (
        ("failed_read", "{ error: { status: 500, title: 'boom' }, data: null }", True),
        ("loaded_empty", "{ error: null, data: { items: [] } }", False),
        ("still_loading", "{ error: null, data: null }", False),
        ("stale_data_kept", "{ error: { status: 503 }, data: { items: [] } }", False),
    ):
        out = json.loads(ctx.eval(f"JSON.stringify(NV_sourceError({shape}))"))
        assert bool(out) is expected, f"{name}: the predicate reads {out}"


def test_the_nothing_matches_gate_is_conditional_on_no_source_failure():
    """G13-01 spec 2: the 'Nothing matches' empty state must not claim
    nothing matches when a source failed to load. The committed gate on
    the render line is evaluated under both shapes: a source-failed
    empty (suppressed) and a true empty (kept)."""
    src = _src()
    m = re.search(
        r"\{(!flat\.length(?: && !anySourceError)?) \? \(\s*\n"
        r"\s*<div className=\"nv-palette-empty\">\s*\n"
        r"\s*Nothing matches \{JSON\.stringify\(q\)\}\.",
        src,
    )
    assert m, "the committed 'Nothing matches' branch gate not found"
    cond = m.group(1)
    assert (
        'data-testid="nv-palette-load-failed"' in src
    ), "no load-failed empty-state copy exists"
    ctx = mr_context("void 0;")
    for name, flat, failed, expected in (
        ("source_failed", "[]", "true", False),
        ("true_empty", "[]", "false", True),
        ("rows_present", "[{ key: 'x' }]", "true", False),
    ):
        ctx.eval(f"var flat = {flat}; var anySourceError = {failed};")
        out = json.loads(ctx.eval(f"JSON.stringify({cond})"))
        assert bool(out) is expected, f"{name}: the Nothing-matches gate reads {out}"


def test_the_input_is_a_combobox_pointing_at_the_active_row():
    """G13-02 spec 1: the input is a combobox whose
    aria-activedescendant names the selected row by the id derived from
    the same row key the row carries (data-row-key)."""
    src = _src()
    i = src.index('data-testid="nv-palette-input"')
    region = src[i - 500 : i + 500]
    assert 'role="combobox"' in region, "the input is not a combobox"
    assert "aria-activedescendant" in region, "the input carries no aria-activedescendant"
    # The id the input points at is derived from the row key (the
    # data-row-key value), so it names the selected row exactly.
    j = src.index("data-row-key={r.key}")
    row_region = src[j - 400 : j + 200]
    assert "NV_paletteRowId(" in row_region, "the rows carry no id for the input to point at"
    assert src.count("NV_paletteRowId(") >= 2, "the input does not reference the row ids"


def test_the_rows_carry_option_role_and_aria_selected():
    """G13-02 spec 2: the rows are options that expose their selection
    state (aria-selected true on the active row)."""
    src = _src()
    i = src.index('data-testid="nv-palette-row"')
    region = src[i - 300 : i + 300]
    assert 'role="option"' in region, "the rows carry no option role"
    assert "aria-selected" in region, "the rows expose no selection state"


def test_recents_404_special_case_is_kept():
    """Guard (intentionally green before AND after the fix): G13-01
    spec item 3 - recents' 404 special case (endpoint not deployed yet
    -> null data, not an error) must survive the error-surface work."""
    assert "err.status === 404" in _src(), "recents' 404 special case was dropped"
