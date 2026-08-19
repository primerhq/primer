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
