"""FD4 — content-visibility row virtualization.

Off-screen div rows + the expensive per-row block content inside table cells
skip layout/paint until scrolled near. content-visibility is a verified no-op
on <tr> (table-internal elements aren't size-containable), so it is applied to
the div-based lists (Studio sidebar rows, activity-event stream) and to the
workers CapacityBar (a <div> inside its <td>, which DOES skip)."""

from pathlib import Path

_UI = Path(__file__).resolve().parents[2] / "ui"
STYLES = (_UI / "styles.css").read_text()
WORKERS = (_UI / "components" / "workers.jsx").read_text()


def test_content_visibility_rules_present() -> None:
    # Div-based long lists get row-level virtualization.
    for sel in (".st-session-row", ".st-file-row", '[data-testid="activity-event"]', ".cv-auto"):
        assert sel in STYLES, f"missing {sel} rule"
    # Each uses content-visibility + a contain-intrinsic-size placeholder so
    # the scrollbar/layout stays stable while rows are skipped.
    cv_count = STYLES.count("content-visibility: auto")
    assert cv_count >= 4, f"expected >=4 content-visibility rules, got {cv_count}"
    assert "contain-intrinsic-size: auto" in STYLES


def test_workers_capacity_bar_is_virtualized() -> None:
    # The per-row capacity segments (up to `capacity` divs each) skip when the
    # row is off-screen — the div-inside-td case that content-visibility covers.
    assert 'className="cv-auto"' in WORKERS
