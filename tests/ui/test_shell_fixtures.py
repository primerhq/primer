"""S8 P1: the designer's input package is a manifest plus one fixture per surface.

The manifest is the contract between this plan and the designer: a fixture
that is not listed is invisible to the handoff, and a listed file that does
not exist is a broken handoff. Both directions are asserted.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "ui" / "fixtures" / "shell"
MANIFEST = FIXTURES / "manifest.json"

DOC_KINDS = ["session", "file", "diff", "wiki"]
OVERLAYS = [
    "providers", "collections", "agents", "graphs", "triggers",
    "toolsets", "tools", "workers", "approvals", "admin",
    "harnesses", "services", "channels", "workspaces",
]


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_exists_and_parses() -> None:
    assert MANIFEST.is_file(), "the designer package needs a manifest"
    man = _manifest()
    assert set(man) == {"surfaces", "doc_kinds", "overlays"}


def test_manifest_pins_the_doc_kinds_and_overlay_names() -> None:
    man = _manifest()
    assert man["doc_kinds"] == DOC_KINDS
    assert man["overlays"] == OVERLAYS


def test_every_listed_surface_file_exists() -> None:
    man = _manifest()
    for entry in man["surfaces"]:
        assert set(entry) == {"id", "file", "describes"}
        assert (FIXTURES / entry["file"]).is_file(), entry["file"]
        assert entry["describes"].strip(), entry["id"]


def test_no_orphan_fixture_files() -> None:
    listed = {e["file"] for e in _manifest()["surfaces"]}
    on_disk = {p.name for p in FIXTURES.glob("*.json")} - {"manifest.json"}
    assert on_disk == listed, f"unlisted: {on_disk - listed}; missing: {listed - on_disk}"


def test_every_fixture_is_valid_json() -> None:
    for path in sorted(FIXTURES.glob("*.json")):
        json.loads(path.read_text(encoding="utf-8"))
