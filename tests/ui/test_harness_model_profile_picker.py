"""Static + transpile checks for the combined LLM provider+model picker widget
used by the harness registration form (HarnessRegisterDialog)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
HARNESS_FORM = UI / "components" / "harness_form.jsx"


def _src() -> str:
    return HARNESS_FORM.read_text(encoding="utf-8")


def test_llm_model_picker_widget_registered() -> None:
    src = _src()
    # The recursive JSON-schema form renders the new combined widget.
    assert '"llm-model-picker"' in src or "'llm-model-picker'" in src
    assert "HF_LlmModelPicker" in src


def test_llm_model_picker_populates_models_from_provider() -> None:
    src = _src()
    # Fetches providers (whose rows carry their model lists) and derives the
    # model options from the selected provider id.
    assert "/v1/llm_providers" in src
    assert ".models" in src
    # The value is the combined { provider_id, model_name } object.
    assert "provider_id" in src and "model_name" in src


def test_llm_model_picker_defaults_to_first_model_on_provider_select() -> None:
    src = _src()
    # Selecting a provider defaults model_name to that provider's first model.
    assert "ms[0]" in src


def test_harness_form_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(
        ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text()
    )
    code = b._transform(_src(), "components/harness_form.jsx")
    assert code and "HF_LlmModelPicker" in code
