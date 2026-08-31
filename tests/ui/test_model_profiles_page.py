"""The model-profiles console page.

The entity exists so ONE model can be registered several times under one
provider with different settings, so the page must treat a shared
model_name as normal rather than as a duplicate.

Static-source checks, matching the rest of the ui/ suite.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "ui" / "components" / "model-profiles.jsx"
APP = ROOT / "ui" / "app.jsx"
INDEX = ROOT / "ui" / "index.html"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


class TestFoldedIn:
    """The standalone page folded into the catalog; the modal did not.

    ModelProfile is LLM-only by design, so a profile belongs under its LLM
    provider rather than on a page of its own. The editor is reused, not
    reimplemented, so this module still guards the modal.
    """

    def test_the_page_component_is_gone(self) -> None:
        src = _src()
        assert "function ModelProfilesPage(" not in src
        assert "window.ModelProfilesPage" not in src

    def test_the_modal_survives_and_is_still_exported(self) -> None:
        src = _src()
        assert "function MP_ProfileModal(" in src
        assert "window.MP_ProfileModal = MP_ProfileModal;" in src

    def test_no_address_or_nav_entry_reaches_it(self) -> None:
        hits = [
            p for p in (ROOT / "ui").rglob("*.js*")
            if 'id: "model-profiles"' in p.read_text(encoding="utf-8")
        ]
        assert hits == [], f"a nav entry still points at it: {hits}"

    def test_nothing_renders_it(self) -> None:
        """The console dispatches through the overlay host now, so that
        is where a surviving mount would be."""
        hits = [
            str(p.relative_to(ROOT)) for p in (ROOT / "ui").rglob("*.jsx")
            if "ModelProfilesPage" in p.read_text(encoding="utf-8")
        ]
        assert hits == [], f"the page is still mounted by: {hits}"

    def test_it_is_still_in_the_bundle_manifest(self) -> None:
        """The file stays: the catalog mounts MP_ProfileModal from it."""
        assert "components/model-profiles.jsx" in INDEX.read_text(encoding="utf-8")

    def test_nothing_still_links_to_the_dead_path(self) -> None:
        for name in ("agents.jsx", "approvals.jsx", "graphs.jsx"):
            src = (ROOT / "ui" / "components" / name).read_text(encoding="utf-8")
            assert '"/model-profiles"' not in src, name
