"""The agent form names a model PROFILE, not a provider+model pair.

Agent.model collapsed to a single ``profile_id`` -- the profile is what
carries the provider, the wire model name, and the API-level config. The
console must send that shape, and it must resolve profiles when rendering
the list, since the agent row alone no longer says which model it runs.

Static-source checks, matching the rest of the ui/ suite.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "agents.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


class TestFormSendsTheProfileShape:
    def test_submit_body_carries_profile_id_only(self) -> None:
        assert "model: { profile_id: profileId }," in _src()

    def test_no_stale_provider_model_pair_remains(self) -> None:
        """A leftover write of the old shape would 422 at the API."""
        src = _src()
        assert "model: { provider_id:" not in src
        assert "agent.model?.provider_id" not in src
        assert "a.model?.model_name" not in src

    def test_picker_lists_profiles_not_providers(self) -> None:
        """uiv2 Wave 2: retargeted from a <select htmlFor="na-model-
        profile"> to the stacked AG_ProfilePicker (mockup's own picker
        style - one bordered row per profile, the bound one tinted) - a
        button-per-row group has no single focusable target for a plain
        <label for>, so the label instead carries its own id and the
        picker its own data-testid."""
        src = _src()
        assert '"/model_profiles?limit=200"' in src
        assert 'id="na-model-profile-label"' in src
        assert "<AG_ProfilePicker" in src

    def test_submit_is_blocked_without_a_profile(self) -> None:
        assert "disabled={!profileId || create.loading}" in _src()

    def test_surfaces_the_profile_field_error(self) -> None:
        assert 'fieldErrors["body.model.profile_id"]' in _src()

    def test_says_the_profile_is_only_a_default(self) -> None:
        """Sessions and chats may override it per run; an operator picking
        a model here should not think it is pinned."""
        assert "DEFAULT model" in _src()

    def test_keeps_a_dangling_profile_selectable(self) -> None:
        """Editing an unrelated field must not silently repoint the agent
        at whatever profile happens to sort first."""
        src = _src()
        assert "profileMissing" in src
        assert "(missing)" in src


class TestListResolvesProfiles:
    def test_list_fetches_profiles(self) -> None:
        assert '"agents:model-profiles"' in _src()

    def test_rows_resolve_provider_through_the_profile(self) -> None:
        src = _src()
        assert "const profile = profileById[profileId];" in src
        assert "const providerId = profile?.provider_id;" in src

    def test_missing_profile_is_shown_not_hidden(self) -> None:
        assert "(missing profile)" in _src()


class TestReferencesPanel:
    def test_checks_the_profile_and_the_provider_behind_it(self) -> None:
        """Either can go missing independently and they are fixed in
        different places, so both get their own row."""
        src = _src()
        assert '"/model_profiles/" + encodeURIComponent(profileId)' in src
        assert "const providerId = profile.data?.provider_id;" in src


def test_agents_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    code = b._transform(_src(), "components/agents.jsx")
    assert code and "AG_NewAgentModal" in code
