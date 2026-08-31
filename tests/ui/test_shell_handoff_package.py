"""S8 P1: the handoff README is the designer's contract, so it is gated.

Section 9 of the spec makes contract changes after handoff a programme
event. That only works if the handoff itself is complete: every surface,
every doc kind, every overlay, and the binding UX rules of section 8.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "ui" / "fixtures" / "shell"
README = FIXTURES / "README.md"

REQUIRED_RULES = [
    "Always-on status verb",
    "Two-phase turn rendering",
    "Scroll anchoring",
    "Per-turn identity chip",
    "Structured decision cards",
    "Rail discipline",
    "Preview tabs",
    "Split editor groups",
    "Dual-render rule",
    "URL-as-state",
    "Hold-to-talk",
    "Attention tiers",
    "First-run walkthrough",
]


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_exists() -> None:
    assert README.is_file()


def test_readme_lists_every_fixture() -> None:
    src = _readme()
    man = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    for entry in man["surfaces"]:
        assert entry["file"] in src, entry["file"]


def test_readme_lists_every_overlay_and_doc_kind() -> None:
    src = _readme()
    man = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    for name in man["overlays"]:
        assert f"`{name}`" in src, name
    for kind in man["doc_kinds"]:
        assert f"`{kind}`" in src, kind


def test_readme_carries_every_binding_ux_rule() -> None:
    src = _readme()
    for rule in REQUIRED_RULES:
        assert rule in src, rule


def test_readme_states_the_url_grammar() -> None:
    src = _readme()
    assert "#/w/{wid}?doc=<kind>:<ref>&overlay=<name>" in src
    assert "overlay=providers:tts:" in src


def test_no_em_dash_in_the_package() -> None:
    # Spelled by codepoint so this guard does not itself introduce the
    # character it forbids (Global Constraints, bullet 1).
    em_dash = chr(0x2014)
    for path in [README] + sorted(FIXTURES.glob("*.json")):
        assert em_dash not in path.read_text(encoding="utf-8"), path.name
