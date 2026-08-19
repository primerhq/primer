"""Regression: drop dev-only spec annotations from the session detail
and wire the dashboard IC tile to the live config endpoint.

Pre-fix, session-detail.jsx rendered two leftover dev annotations to
end users: a 'Reads are authoritative — known to drift after signals'
info banner and an inline 'does not gate on status — pinned spec §12'
hint next to the Steer instruction field. Both referenced internal
ticket ids the user has no context for.

Pre-fix, app.jsx derived `subsystemOn` from a tweaks-panel toggle,
so the dashboard IC tile showed OFF even when the subsystem was
configured and active. Now it polls
`/v1/internal_collections/config` and treats `activated_at` as the
source of truth.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def test_no_reads_are_authoritative_banner() -> None:
    src = (UI / "components" / "session-detail.jsx").read_text(encoding="utf-8")
    assert "Reads are authoritative" not in src
    assert "T0399" not in src
    assert "T0555" not in src
    assert "T0611" not in src


def test_no_steer_pinned_spec_hint() -> None:
    src = (UI / "components" / "session-detail.jsx").read_text(encoding="utf-8")
    assert "does not gate on status" not in src
    assert "pinned spec" not in src


def test_ic_state_derives_from_the_live_probe() -> None:
    """The console must read the subsystem's own answer, never a stale
    tweaks toggle. The surface that shows it moved; the rule did not."""
    src = (UI / "components" / "internal-collections.jsx").read_text(encoding="utf-8")
    assert "/internal_collections/config" in src
    assert "activated_at" in src


def test_app_subsystem_on_not_tweak_only() -> None:
    src = (UI / "app.jsx").read_text(encoding="utf-8")
    # The exact line that sourced subsystemOn from the tweak toggle.
    assert "subsystemOn = !!tweaks.subsystemOn" not in src, (
        "subsystemOn must not be read from the tweaks toggle — wire "
        "it through the live /internal_collections/config probe"
    )


def test_no_surface_hardcodes_a_bootstrap_age() -> None:
    """The dashboard that carried this string is gone; the rule is what
    survives -- bootstrap state is derived from icConfig, never typed in."""
    hits = [
        str(p.relative_to(UI)) for p in UI.rglob("*.js*")
        if "last bootstrap 14m ago" in p.read_text(encoding="utf-8")
    ]
    assert hits == [], f"hardcoded bootstrap age in: {hits}"
