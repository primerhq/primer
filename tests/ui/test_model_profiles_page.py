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
ROUTER = ROOT / "ui" / "foundation" / "router.js"
CHROME = ROOT / "ui" / "components" / "chrome.jsx"
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

    def test_the_route_and_nav_entry_are_gone(self) -> None:
        assert '"/model-profiles"' not in ROUTER.read_text(encoding="utf-8")
        assert 'id: "model-profiles"' not in CHROME.read_text(encoding="utf-8")

    def test_the_app_no_longer_dispatches_to_it(self) -> None:
        app = APP.read_text(encoding="utf-8")
        assert 'root === "model-profiles"' not in app
        assert "<ModelProfilesPage />" not in app
        assert '"model-profiles": "/model-profiles"' not in app

    def test_it_is_still_in_the_bundle_manifest(self) -> None:
        """The file stays: the catalog mounts MP_ProfileModal from it."""
        assert "components/model-profiles.jsx" in INDEX.read_text(encoding="utf-8")

    def test_nothing_still_links_to_the_dead_path(self) -> None:
        for name in ("agents.jsx", "approvals.jsx", "graphs.jsx"):
            src = (ROOT / "ui" / "components" / name).read_text(encoding="utf-8")
            assert '"/model-profiles"' not in src, name
