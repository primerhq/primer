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


class TestWiring:
    def test_route_is_registered(self) -> None:
        assert '"/model-profiles"' in ROUTER.read_text(encoding="utf-8")

    def test_sidebar_entry_exists_and_is_admin_only(self) -> None:
        chrome = CHROME.read_text(encoding="utf-8")
        assert 'id: "model-profiles"' in chrome
        # A profile names a provider and its tunables => provider config.
        entry = chrome.split('id: "model-profiles"', 1)[1][:120]
        assert "adminOnly: true" in entry

    def test_app_dispatches_to_the_page(self) -> None:
        app = APP.read_text(encoding="utf-8")
        assert 'root === "model-profiles"' in app
        assert "<ModelProfilesPage />" in app

    def test_is_in_the_bundle_manifest(self) -> None:
        """index.html is the source of truth for transpile + load order."""
        assert "components/model-profiles.jsx" in INDEX.read_text(encoding="utf-8")

    def test_exports_on_window(self) -> None:
        assert "window.ModelProfilesPage = ModelProfilesPage;" in _src()


class TestPageBehaviour:
    def test_routes_io_through_the_hook_layer(self) -> None:
        """Pages must not call apiFetch outside a hook -- that bypasses
        polling, dedupe, cancellation, and stale-while-error."""
        src = _src()
        assert "useResource(" in src and "useMutation(" in src

    def test_renders_loading_error_and_empty_states(self) -> None:
        src = _src()
        assert "list.loading" in src
        assert "list.error" in src
        assert "No model profiles yet" in src

    def test_has_a_mobile_adaptation(self) -> None:
        src = _src()
        assert "useViewport" in src and "card-list" in src

    def test_poll_pauses_while_filtering(self) -> None:
        assert "pauseWhile: () => filterFocused" in _src()

    def test_harness_managed_rows_hide_mutation(self) -> None:
        """Mirrors the backend's 409-on-public-CRUD discipline."""
        assert "!r.harness_id && (" in _src()

    def test_delete_conflict_renders_inline_not_as_a_toast(self) -> None:
        """A 409 means an agent still points at it; the operator needs to
        act on that without losing the dialog."""
        src = _src()
        assert "setDeleteErr" in src
        assert "{deleteErr && " in src

    def test_reasoning_offers_the_full_neutral_scale(self) -> None:
        src = _src()
        assert 'MP_REASONING_LEVELS = ["off", "minimal", "low", "medium", "high"]' in src

    def test_states_the_vendor_caveats(self) -> None:
        """Not every vendor has a true off, and vLLM's Responses endpoint
        ignores the setting entirely -- an operator setting this needs to
        know before wondering why nothing changed."""
        src = _src()
        assert "floors at" in src and "openchat" in src
