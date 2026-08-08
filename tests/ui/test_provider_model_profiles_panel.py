"""The LLM provider detail page's model-profile panel.

An LLM provider has no models[] of its own -- what it serves is whatever
ModelProfile rows point at it. "Fetch models" live-probes the upstream and
offers each reported model as a profile to create: discovery says what
exists, a profile says what this deployment chose to register.

Static-source checks, matching the rest of the ui/ suite.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "providers.jsx"
INDEX = UI / "index.html"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


class TestCreateForm:
    def test_llm_providers_do_not_submit_a_models_list(self) -> None:
        """The field was removed from LLMProvider; sending it is a silently
        discarded edit."""
        src = _src()
        assert "...(usesProfiles ? {} : { models: cleanModels() })," in src

    def test_llm_providers_are_saveable_with_no_models(self) -> None:
        """The old gate required at least one model row, which made LLM
        providers uncreatable once the field went away."""
        assert "&& (usesProfiles || models.length > 0)" in _src()

    def test_embedding_providers_keep_their_models_list(self) -> None:
        """Only the LLM family moved to profiles."""
        src = _src()
        assert 'const usesProfiles = fieldKind === "llm";' in src
        assert "models: cleanModels()" in src


class TestProfilesPanel:
    def test_detail_page_uses_the_panel_for_llm(self) -> None:
        src = _src()
        assert 'k.plural === "llm_providers" ? (' in src
        assert "<PR_LlmProfilesPanel providerId={p.id}" in src

    def test_probe_targets_the_saved_row_not_a_replayed_config(self) -> None:
        """The row this page holds has its secrets redacted, so replaying
        its config would authenticate with the redaction."""
        assert '"/llm_providers/" + encodeURIComponent(providerId) + "/discovered_models"' in _src()

    def test_id_synthesis_matches_the_migration(self) -> None:
        """A profile created here must collide with, not duplicate, one the
        m002 migration would have made."""
        from primer.storage.migrations.m002_model_profiles import synth_profile_id

        assert synth_profile_id("gx10", "Qwen/Qwen3-32B") == "gx10--qwen-qwen3-32b"
        # The console mirrors the same rule.
        src = _src()
        assert "function pr_synthProfileId(providerId, modelName)" in src
        assert "`${providerId}--${slug}`" in src

    def test_creating_several_profiles_at_once_is_possible(self) -> None:
        """The whole point of the entity is many profiles per provider."""
        src = _src()
        assert "const createPicked = async () =>" in src
        assert "picked.size === 0" in src

    def test_partial_batch_failure_reports_what_landed(self) -> None:
        """Re-fetching after registering some models is the normal case, so
        duplicate ids must not fail the whole batch."""
        src = _src()
        assert "failed.push(" in src
        assert "Created ${made} profile" in src

    def test_already_registered_models_stay_selectable(self) -> None:
        """A second profile for the same model is the point of profiles."""
        src = _src()
        assert "already registered" in src
        assert "haveModel.has(m.name)" in src

    def test_per_row_customize_opens_a_prefilled_create(self) -> None:
        src = _src()
        assert "setCustomizing(m)" in src
        assert "prefill={customizing.name ? {" in src

    def test_a_provider_with_no_live_list_can_still_get_profiles(self) -> None:
        """Probing "aggregated" 400s -- its virtual model name only ever
        exists here, so the panel must not gate profile creation on a
        successful fetch."""
        src = _src()
        assert 'setCustomizing({ name: "" })' in src
        assert "disabled={discover.loading || !discoverable}" in src

    def test_modal_is_defined_before_this_file_in_the_bundle(self) -> None:
        html = INDEX.read_text(encoding="utf-8")
        assert html.index("components/model-profiles.jsx") < html.index(
            "components/providers.jsx"
        )


def test_providers_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    code = b._transform(_src(), "components/providers.jsx")
    assert code and "PR_LlmProfilesPanel" in code


class TestListCounts:
    def test_llm_list_counts_profiles_not_the_removed_field(self) -> None:
        """Counting p.models would report 0 for every LLM provider."""
        src = _src()
        assert 'const isLlm = k.plural === "llm_providers";' in src
        assert "profileCounts[row.id]" in src

    def test_column_is_labelled_for_what_it_counts(self) -> None:
        assert '{isLlm ? "Profiles" : "Models"}' in _src()
