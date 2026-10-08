"""The provider modal's "schema-driven from /providers/_types" pill is gone too (follow-up asks on #483, lead 2026-10-08).

#483 removed the "verb:" chips and said in the docs that dialogs carry no developer annotations. The provider create/edit modal still drew a
mono-spaced pill beside its title reading "schema-driven from /providers/_types", which is a developer's note in the same way (a user cannot
act on an internal endpoint path). The comment in the overlays file that explained the bypass chrome also still said "title/verb-chip/close".
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CATALOG = (UI / "components" / "provider-catalog.jsx").read_text(encoding="utf-8")
OVERLAYS = (UI / "components" / "console" / "nv-overlays.jsx").read_text(encoding="utf-8")
STYLES = (UI / "styles.css").read_text(encoding="utf-8")


def test_the_provider_modal_draws_no_schema_pill() -> None:
    assert "schema-driven from" not in CATALOG
    assert "provider-modal-schema-chip" not in CATALOG


def test_the_pills_style_went_with_it() -> None:
    assert ".pc-modal-chip-inline" not in STYLES


def test_the_modal_title_is_the_plain_title() -> None:
    start = CATALOG.index("`New ${formKlass.key} provider")
    window = CATALOG[start - 400:start + 400]
    assert "React.Fragment" not in window, "the title is a string again, which also lets the dialog be named by it"


def test_no_comment_still_describes_a_verb_chip() -> None:
    assert "verb-chip" not in OVERLAYS
