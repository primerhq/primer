"""The transition banner and its programme summary (S9 section 3).

While main carries the revamp, README and the docs landing must say so.
The banner is keyed on the version so it cannot outlive the transition:
below 2.0.0 it is required, at 2.0.0 and above it is forbidden.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"
SUMMARY = REPO / "docs" / "ux-revamp.md"

BANNER_START = "<!-- transition-banner:start -->"
BANNER_END = "<!-- transition-banner:end -->"

EM_DASH = "—"  # escaped: the literal character is banned repo-wide


def _version() -> tuple[int, int, int]:
    with (REPO / "pyproject.toml").open("rb") as fh:
        raw = tomllib.load(fh)["project"]["version"]
    major, minor, patch = re.match(r"^(\d+)\.(\d+)\.(\d+)", raw).groups()
    return int(major), int(minor), int(patch)


def test_summary_page_exists() -> None:
    assert SUMMARY.exists(), f"missing programme summary at {SUMMARY}"


def test_summary_page_has_no_em_dash() -> None:
    assert EM_DASH not in SUMMARY.read_text(encoding="utf-8")


def test_summary_page_names_every_spec() -> None:
    text = SUMMARY.read_text(encoding="utf-8")
    for spec in (
        "Core session model",
        "Collections v2",
        "Client tools",
        "Provider platform",
        "Bootstrap and operator",
        "Triggers and channels",
        "Observability",
        "Fresh shell",
        "Cutover",
    ):
        assert spec in text, f"summary does not name the {spec} spec"


def test_summary_page_states_the_stable_line_and_the_target() -> None:
    text = SUMMARY.read_text(encoding="utf-8")
    assert "v0.6.x" in text, "summary does not name the last stable pre-revamp line"
    assert "v2.0.0" in text, "summary does not name the completion tag"


def _banner(text: str) -> str | None:
    if BANNER_START not in text or BANNER_END not in text:
        return None
    return text.split(BANNER_START, 1)[1].split(BANNER_END, 1)[0]


def test_readme_carries_the_banner_before_v2() -> None:
    major, _minor, _patch = _version()
    body = _banner(README.read_text(encoding="utf-8"))
    if major >= 2:
        assert body is None, "v2.0.0 shipped: the transition banner must be gone"
        return
    assert body is not None, "README is missing the transition banner markers"
    assert "mid-revamp" in body
    assert "v0.6.x is the last stable pre-revamp release" in body
    assert "v2.0.0" in body
    assert "docs/ux-revamp.md" in body


def test_readme_banner_sits_above_the_first_rule() -> None:
    """The banner is the first thing under the hero, not buried mid-page."""
    text = README.read_text(encoding="utf-8")
    if _version()[0] >= 2:
        return
    assert text.index(BANNER_START) < text.index("\n## Why Primer")


def test_transition_versions_stay_below_v2() -> None:
    """While the banner is up, the release line is a 0.x pre-release.

    Spec section 3: transition releases are 0.x with a banner; v2.0.0 tags
    only when S8's flag day completes and the banner comes off.
    """
    major, _minor, _patch = _version()
    banner_present = _banner(README.read_text(encoding="utf-8")) is not None
    if banner_present:
        assert major == 0, (
            f"banner is up but the version is {major}.x; "
            "transition releases must stay on the 0.x line"
        )
    else:
        assert major >= 2, (
            f"banner is down but the version is {major}.x; "
            "the banner only comes off at v2.0.0"
        )


def test_summary_link_target_exists() -> None:
    """The banner's link is a real repo path, not a dead one."""
    assert SUMMARY.exists()
    assert SUMMARY.relative_to(REPO).as_posix() == "docs/ux-revamp.md"
