"""Static + transpile checks for the model-profile picker widget used by the
harness registration form (HarnessRegisterDialog).

An agent's model is a single ModelProfile id -- the profile carries the
provider, the wire model name and the API-level config -- so remapping one
on install is one choice, not a provider + model pair.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
HARNESS_FORM = UI / "components" / "harness_form.jsx"


def _src() -> str:
    return HARNESS_FORM.read_text(encoding="utf-8")


def test_model_profile_picker_widget_registered() -> None:
    src = _src()
    # The recursive JSON-schema form renders the widget.
    assert '"model-profile-picker"' in src or "'model-profile-picker'" in src
    assert "HF_ModelProfilePicker" in src


def test_widget_name_is_one_the_backend_accepts() -> None:
    """A widget the OverrideMapping Literal rejects can never be rendered."""
    from primer.model.harness import OverrideMapping

    OverrideMapping(
        field_path="/model/profile_id",
        override_path="/agent_model_profile",
        widget="model-profile-picker",
    )


def test_picker_lists_profiles() -> None:
    src = _src()
    assert "/v1/model_profiles" in src
    # The value is a bare profile id, not a { provider_id, model_name } pair.
    assert "typeof value === \"string\"" in src


def test_picker_shows_the_resolved_provider_and_model() -> None:
    """Several profiles may share one model, so the id alone is ambiguous."""
    src = _src()
    assert "pr.provider_id" in src and "pr.model_name" in src


def test_picker_keeps_a_missing_saved_id_visible() -> None:
    src = _src()
    assert "missing &&" in src


def test_harness_form_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(
        ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text()
    )
    code = b._transform(_src(), "components/harness_form.jsx")
    assert code and "HF_ModelProfilePicker" in code
