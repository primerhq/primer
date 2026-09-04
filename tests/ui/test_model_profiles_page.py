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

    01a067c4: an aggregated profile has no provider_id, so it cannot live
    under any one provider's page the way a single profile does. Its home
    is MP_AllProfilesPanel, mounted as a provider-catalog "class" chip
    (form:"panel", same precedent as the ssp/workspace/channel classes) --
    NOT a resurrection of the banned ModelProfilesPage below. The ban was
    about a separate PAGE/nav entry breaking the providers one-page IA
    doctrine; a class chip INSIDE that same page satisfies the doctrine
    (profiles, single or aggregated, stay reachable only from the
    providers page) rather than violating it. Do not read the new chip as
    contradicting this test class.
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


class TestAggregatedKindSupport:
    """01a067c4: MP_ProfileModal/MP_ProfileCard support kind="aggregated"
    profiles -- an ordered pool of other "single" profiles with a
    routing/failover policy -- alongside the pre-existing single shape.
    """

    def test_kind_toggle_is_gated_on_allProfiles(self) -> None:
        """Per-provider PC_ProfilesPanel (provider-catalog.jsx) mounts
        this modal WITHOUT allProfiles, exactly as it always has (its own
        call site is untouched by this change) -- so the toggle must not
        appear there, degrading that caller to its old single-kind-only
        behaviour automatically."""
        src = _src()
        assert "supportsAggregated = Array.isArray(allProfiles)" in src
        assert "supportsAggregated && (" in src

    def test_kind_toggle_locks_on_edit(self) -> None:
        """Converting an existing row's kind in place is not modelled as
        an edit -- delete and recreate under the intended kind."""
        src = _src()
        assert "!isEdit && setKind" in src

    def test_member_picker_excludes_self_and_aggregated_candidates(self) -> None:
        src = _src()
        assert 'p.kind !== "aggregated"' in src
        assert "p.id !== id" in src
        assert "!members.includes(p.id)" in src

    def test_members_are_reorderable(self) -> None:
        """Order is the failover order, so it has to be editable."""
        src = _src()
        assert "const moveMember =" in src

    def test_routing_and_failover_controls_present(self) -> None:
        """Enum strings match the backend StrEnums exactly (relocated
        from the deleted provider-aggregated-editor.jsx's own test)."""
        src = _src()
        for token in (
            "round_robin", "sequential", "before_first_token", "mid_stream",
            "transient", "transient_and_config",
        ):
            assert token in src, f"{token} missing"

    def test_submit_body_branches_on_kind(self) -> None:
        src = _src()
        assert 'kind: "aggregated"' in src
        assert "strategy," in src
        assert "failover_point: failoverPoint," in src
        assert "failover_on: failoverOn," in src

    def test_minimum_two_members_is_surfaced_in_the_save_gate(self) -> None:
        """The backend rejects < 2 members (ruling 6); the modal's own
        Save gate mirrors it so a submit is not the first time an
        operator learns this."""
        src = _src()
        assert "members.length >= 2" in src

    def test_card_shows_a_kind_chip_for_aggregated_rows(self) -> None:
        src = _src()
        assert "function MP_KindChip(" in src

    def test_card_shows_member_count_not_provider_model_for_aggregated(self) -> None:
        src = _src()
        assert "isAggregated" in src
        assert "members.length" in src

    def test_it_transpiles(self) -> None:
        from primer.api._jsx_bundle import JSXBundler

        b = JSXBundler(
            ui_dir=ROOT / "ui",
            babel_source=(ROOT / "ui" / "vendor" / "babel.min.js").read_text(),
        )
        code = b._transform(_src(), "components/model-profiles.jsx")
        assert code and "MP_AllProfilesPanel" in code


class TestAllProfilesPanel:
    """The provider-agnostic browse surface -- an aggregated profile's
    only home, since it has no provider_id to be scoped under."""

    def test_panel_exists_and_is_exported(self) -> None:
        src = _src()
        assert "function MP_AllProfilesPanel(" in src
        assert "window.MP_AllProfilesPanel = MP_AllProfilesPanel;" in src

    def test_panel_fetches_all_profiles_unscoped(self) -> None:
        src = _src()
        assert '"/model_profiles?limit=200"' in src

    def test_panel_passes_allProfiles_to_the_modal(self) -> None:
        src = _src()
        assert "allProfiles={allProfiles}" in src

    def test_registered_as_a_provider_catalog_class(self) -> None:
        catalog = (ROOT / "ui" / "components" / "provider-catalog.jsx").read_text(encoding="utf-8")
        assert 'key: "model_profile"' in catalog
        assert "panel: () => window.MP_AllProfilesPanel" in catalog

    def test_the_new_class_key_is_not_the_banned_nav_id(self) -> None:
        """TestFoldedIn's ban greps for the literal `id: "model-profiles"`
        (hyphenated, a nav-entry id shape). This class uses a distinct
        `key: "model_profile"` (underscored, singular) -- a
        provider-catalog class key, not a nav entry -- so it does not
        trip that check."""
        catalog = (ROOT / "ui" / "components" / "provider-catalog.jsx").read_text(encoding="utf-8")
        assert 'id: "model-profiles"' not in catalog


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
