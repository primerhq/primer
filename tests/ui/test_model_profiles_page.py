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
    """The standalone page folded away; the modal did not.

    ModelProfile is LLM-only by design. Every profile, single or
    aggregated, lives on the LLM Profiles page (Platform > LLM Profiles,
    the `profiles` entry of NV_PLAT_PAGES), whose cards open
    MP_ProfileModal through NV_ProfileModalHost (Platform revamp Plan 03).
    That page is a Platform nav entry, not the banned ModelProfilesPage
    below: the ban is about a second page component with its own address,
    and this module still guards that it does not come back.
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
        """The file stays: the Platform page's NV_ProfileModalHost mounts MP_ProfileModal from it."""
        assert "components/model-profiles.jsx" in INDEX.read_text(encoding="utf-8")

    def test_nothing_still_links_to_the_dead_path(self) -> None:
        for name in ("agents.jsx", "approvals.jsx", "graphs.jsx"):
            src = (ROOT / "ui" / "components" / name).read_text(encoding="utf-8")
            assert '"/model-profiles"' not in src, name


    def test_the_new_class_key_is_not_the_banned_nav_id(self) -> None:
        """test_no_address_or_nav_entry_reaches_it greps for the literal
        `id: "model-profiles"` (hyphenated, a nav-entry id shape). The
        provider catalog never carries that id (moved here from the
        retired TestAllProfilesPanel, Plan 03 Task 6)."""
        catalog = (ROOT / "ui" / "components" / "provider-catalog.jsx").read_text(encoding="utf-8")
        assert 'id: "model-profiles"' not in catalog

class TestAggregatedKindSupport:
    """01a067c4: the profile overlays support kind="aggregated" profiles
    -- an ordered pool of other "single" profiles with a routing/failover
    policy -- alongside the pre-existing single shape. Plan 03 Task 6
    retired the card source pins (the kind chip, the member count): the
    LLM Profiles page's card says "aggregated" and "N profiles · ..."
    (tests/ui/test_llm_profiles_page.py).

    Platform revamp Plan 03: the aggregated form is its own overlay,
    MP_AggregateOverlay (a PK_FormOverlay on the platform kit), which the
    hook-free MP_ProfileModal dispatcher hands an aggregated or a new profile
    to. There is no Single/Aggregated kind toggle any more, so the source
    pins of the toggle (its gate on allProfiles, its lock on edit, the
    candidate filter, the reorder function, the body branch on kind and the
    save gate) are gone; what they guarded is pinned by behaviour in
    tests/ui/test_llm_profile_overlays.py and tests/ui/test_llm_profiles_helpers.py.
    """

    def test_routing_and_failover_controls_present(self) -> None:
        """Enum strings match the backend StrEnums exactly (relocated
        from the deleted provider-aggregated-editor.jsx's own test)."""
        src = _src()
        for token in (
            "round_robin", "sequential", "before_first_token", "mid_stream",
            "transient", "transient_and_config",
        ):
            assert token in src, f"{token} missing"

    def test_it_transpiles(self) -> None:
        from primer.api._jsx_bundle import JSXBundler

        b = JSXBundler(
            ui_dir=ROOT / "ui",
            babel_source=(ROOT / "ui" / "vendor" / "babel.min.js").read_text(),
        )
        code = b._transform(_src(), "components/model-profiles.jsx")
        assert code and "MP_AggregateOverlay" in code


class TestAggregatedEditorRemovedFromProviderForm:
    """01a067c4: the aggregated concept moved off LLMProvider onto
    ModelProfile -- provider-form.jsx no longer has a variant branch for
    it, and provider-aggregated-editor.jsx (its dedicated editor) is
    deleted outright, not orphaned."""

    def test_provider_aggregated_editor_file_is_gone(self) -> None:
        assert not (ROOT / "ui" / "components" / "provider-aggregated-editor.jsx").exists()

    def test_provider_form_has_no_aggregated_variant_branch(self) -> None:
        form = (ROOT / "ui" / "components" / "provider-form.jsx").read_text(encoding="utf-8")
        assert "PC_AggregatedMount" not in form
        assert 'shape.variant === "aggregated"' not in form

    def test_index_html_no_longer_registers_the_deleted_file(self) -> None:
        html = INDEX.read_text(encoding="utf-8")
        assert "provider-aggregated-editor.jsx" not in html
