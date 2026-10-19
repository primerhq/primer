"""Platform wave P4: provider-catalog.jsx consumes what the P2/P3
backend waves built - reachable/unreachable badges + last-probe/error
fact rows on PC_InstanceCard (virgin rows badge-less).

The panel half of this file (PC_ProfilesPanel wired to real
MP_ProfileCard/MP_ProfilesGrid cards with a fixed MP_ProfileModal
invocation and a provider-down join) left with the panel when profiles
moved to the LLM Profiles page (plan 02 task 8, design 7 and B5; plan 03
pins the panel where it lives now).

Static-source checks only (the tests/ui suite convention). Sister of
test_provider_catalog_profiles.py (the catalog's no-panel pins) and
test_model_profiles_p4.py (the card-side half of the badge wiring).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "provider-catalog.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _card_src() -> str:
    src = _src()
    start = src.index("function PC_InstanceCard(")
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


# ---- Reachability badge + fact rows ---------------------------------------


def test_virgin_rows_render_no_reachability_badge() -> None:
    card = _card_src()
    assert "const virgin = row.last_probe_at == null;" in card
    # PC_ReachabilityBadge itself lives above PC_InstanceCard; check its
    # own early-return there instead.
    src = _src()
    badge_fn_start = src.index("function PC_ReachabilityBadge(")
    badge_fn_end = src.index("\n}", badge_fn_start)
    badge_fn = src[badge_fn_start:badge_fn_end]
    assert "if (lastProbeAt == null) return null;" in badge_fn


def test_reachable_and_unreachable_are_derived_from_last_probe_ok() -> None:
    src = _src()
    badge_fn_start = src.index("function PC_ReachabilityBadge(")
    badge_fn_end = src.index("\n}", badge_fn_start)
    badge_fn = src[badge_fn_start:badge_fn_end]
    assert '"reachable" : "unreachable"' in badge_fn


def test_card_shows_last_probed_and_error_fact_rows() -> None:
    """RETARGET (01a063ab): facts became a `{key, value}` array feeding a
    real two-column CSS grid (the designer mockup's aligned key-value
    card) - "last probed" (a verb-phrase label) became the noun-phrase
    "last probe" to match its siblings' key style ("models", "default
    for", "key", "error"). Same information, consistent column heading."""
    card = _card_src()
    assert '"last probe"' in card
    assert "row.last_error" in card
    # The testid is now assigned per-fact via a ternary (only the "error"
    # fact row carries it), not a literal per-field JSX attribute.
    assert '`provider-card-probe-error-${row.id}` : undefined' in card


def test_error_row_only_renders_when_actually_unreachable() -> None:
    """RETARGET (01a063ab): the fact set is now built by an if(!unreachable)
    / else branch (reachable and unreachable rows show different fact
    sets entirely, matching the mockup's own two examples) rather than a
    single flat list each fact self-guards within - the error fact is
    still only ever pushed from inside the `unreachable` branch."""
    card = _card_src()
    else_branch = card[card.index("} else {"):card.index("\n  }\n\n  return (")]
    assert "if (row.last_error) {" in else_branch
    assert 'facts.push({ key: "error"' in else_branch


def test_bundle_transpiles_with_provider_catalog_p4() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/provider-catalog.jsx === */" in text
